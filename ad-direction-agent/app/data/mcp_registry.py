"""MCP Server & Tool Registry — 按 tool 路由到对应 server 的 invoker。

服务注册：一个 MCP Server 一套连接配置（endpoint / 鉴权 / 连接池 / 并发阀）。
工具注册：每工具登记所属 server。下游只传 tool_name，不感知 server。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from app.config.settings import settings
from app.data.mcp_client import StreamableHttpMcpInvoker

logger = logging.getLogger(__name__)


# ── 数据模型 ──────────────────────────────────────────────────────────────────

@dataclass
class McpServerConfig:
    server_id: str                        # "starrocks" / "azlisting"
    endpoint: str                         # MCP gateway URL
    token: str = ""
    header_name: str = "Authorization"
    timeout: float = 1200.0
    max_connections: int = 120
    max_in_flight: int = 115              # 服务级并发阀
    retries: int = 1


@dataclass
class McpToolRegistration:
    tool_name: str
    server_id: str


# ── 服务运行时 ────────────────────────────────────────────────────────────────

class McpServerRuntime:
    """懒创建 invoker + 服务级 Semaphore（跨 McpAdapter 实例共享）。"""

    def __init__(self, config: McpServerConfig) -> None:
        self.config = config
        self._invoker: StreamableHttpMcpInvoker | None = None
        self._sem = asyncio.Semaphore(max(1, config.max_in_flight))

    def get_invoker(self) -> StreamableHttpMcpInvoker:
        if self._invoker is None:
            if not self.config.endpoint:
                raise RuntimeError(
                    f"MCP 服务 [{self.config.server_id}] endpoint 未配置，"
                    "请在 .env 中设置对应的 MCP URL"
                )
            self._invoker = StreamableHttpMcpInvoker(
                endpoint=self.config.endpoint,
                token=self.config.token if self.config.token else None,
                header_name=self.config.header_name,
                timeout=self.config.timeout,
                max_connections=self.config.max_connections,
            )
        return self._invoker

    async def aclose(self) -> None:
        if self._invoker is not None:
            await self._invoker.aclose()
            self._invoker = None


# ── 注册表 ────────────────────────────────────────────────────────────────────

class ToolRegistry:
    def __init__(self) -> None:
        self._servers: dict[str, McpServerConfig] = {}
        self._runtimes: dict[str, McpServerRuntime] = {}
        self._tools: dict[str, McpToolRegistration] = {}

    def register_server(self, config: McpServerConfig) -> None:
        if config.server_id in self._servers:
            raise ValueError(f"MCP 服务重复注册: {config.server_id}")
        self._servers[config.server_id] = config
        logger.info("MCP 服务注册 [%s] endpoint=%s", config.server_id, config.endpoint)

    def register_tool(self, tool_name: str, server_id: str) -> None:
        if server_id not in self._servers:
            raise ValueError(f"工具 [{tool_name}] 所属服务 [{server_id}] 未注册")
        if tool_name in self._tools:
            raise ValueError(f"MCP 工具重复注册: {tool_name}")
        self._tools[tool_name] = McpToolRegistration(tool_name=tool_name, server_id=server_id)

    def resolve(self, tool_name: str) -> McpServerRuntime:
        reg = self._tools.get(tool_name)
        if reg is None:
            raise ValueError(f"MCP 工具未注册: {tool_name}")
        config = self._servers[reg.server_id]
        if reg.server_id not in self._runtimes:
            self._runtimes[reg.server_id] = McpServerRuntime(config)
        return self._runtimes[reg.server_id]

    def get_server_id(self, tool_name: str) -> str:
        reg = self._tools.get(tool_name)
        return reg.server_id if reg else "unknown"

    async def shutdown(self) -> None:
        for sid, rt in self._runtimes.items():
            await rt.aclose()
            logger.info("MCP 服务关闭 [%s]", sid)


registry = ToolRegistry()


# ── 启动注册 ──────────────────────────────────────────────────────────────────

# SR: 映射现有 mcp_* 全局配置
registry.register_server(McpServerConfig(
    server_id="starrocks",
    endpoint=settings.mcp_gateway_url,
    token=settings.mcp_gateway_token,
    header_name=settings.mcp_gateway_header_name,
    timeout=settings.mcp_timeout,
    max_connections=settings.mcp_max_connections,
    max_in_flight=settings.mcp_max_concurrency,
    retries=settings.mcp_retries,
))

# AZ: 独立配置（核心词离线进程使用，在线主服务预置）
registry.register_server(McpServerConfig(
    server_id="azlisting",
    endpoint=settings.azlisting_mcp_url,
    token=settings.azlisting_mcp_token,
    header_name=settings.azlisting_mcp_header_name,
    timeout=settings.azlisting_mcp_timeout,
    max_connections=settings.azlisting_mcp_max_connections,
    max_in_flight=settings.azlisting_mcp_max_in_flight,
))

# ── SR 工具注册 ──
_STARROCKS_TOOLS = [
    # TOOL_ARG_BUILDERS
    "parent_listing_detail", "listing_basic_info_v2", "parent_listing_stock_summary",
    "product_sales", "ad_product_report", "ad_placement_report",
    "ad_search_term_report", "ad_keyword_report",
    "flow_keywords", "own_keyword_flow",
    "direct_competitors", "keyword_competitors", "keyword_child_asins",
    "ad_campaign_product_keyword_list",
    # CAMPAIGN_TOOLS
    "ad_campaign_list", "ad_campaign_basic_info_v2", "ad_campaign_basic_info",
    "ad_campaign_product_report", "ad_campaign_placement_report",
    "ad_campaign_search_term_report", "ad_portfolio_list",
    # 直接调用（campaign_fetcher）
    "seller_sprite_keyword_reverse", "whp_amazon_advert_keyword_suggest_bid",
]
for t in _STARROCKS_TOOLS:
    registry.register_tool(t, "starrocks")

# ── AZ 工具注册 ──
_AZ_TOOLS = [
    "erp_listing_asin_keyword_rank_history",
]
for t in _AZ_TOOLS:
    registry.register_tool(t, "azlisting")

"""广告调整 MCP 客户端（whp-advert-agent）— 真实执行广告调整。

与数据服务器 MCP（user-starrocks-data-server）分离：独立 endpoint + token + session。
复用 mcp_client.StreamableHttpMcpInvoker 传输层（initialize/tools/call + SSE 解析 + 会话复用）。

6 个工具：
  agent_async_batch_update_advert     异步批量改已有广告 → 返回 taskId
  agent_batch_update_advert_result    查异步结果
  agent_create_portfolio_campaign     新建组合(可选)+活动
  agent_create_negative_keywords      否定词/关键词
  agent_query_portfolio_list          查组合列表
  agent_query_keyword_suggest_bid     查建议竞价
"""

from __future__ import annotations

import logging
from typing import Any

from app.config.settings import settings
from app.data.mcp_client import McpClientError, StreamableHttpMcpInvoker

logger = logging.getLogger(__name__)


class AdvertMcpClient:
    """广告调整 MCP 薄封装。每次新建独立 invoker（会话短命，调完即弃）。"""

    def __init__(
        self,
        *,
        url: str | None = None,
        token: str | None = None,
        timeout: float | None = None,
    ) -> None:
        self._url = (url or settings.advert_mcp_url or "").strip()
        self._token = token if token is not None else settings.advert_mcp_token
        self._timeout = timeout or settings.advert_mcp_timeout
        self._invoker: StreamableHttpMcpInvoker | None = None

    def _get_invoker(self) -> StreamableHttpMcpInvoker:
        if not self._url:
            raise McpClientError("广告调整 MCP 未配置: advert_mcp_url")
        if not self._token:
            raise McpClientError("广告调整 MCP 未配置 token: advert_mcp_token")
        if self._invoker is None:
            self._invoker = StreamableHttpMcpInvoker(
                endpoint=self._url,
                token=self._token,
                header_name="Authorization",
                timeout=self._timeout,
                max_connections=8,
            )
        return self._invoker

    async def call(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        """通用工具调用（结果已 unwrap）。"""
        invoker = self._get_invoker()
        logger.info("Advert MCP call: %s args_keys=%s", tool_name, list(arguments.keys()))
        return await invoker.call_tool(tool_name, arguments)

    # ── 写工具（真实调整）──────────────────────────────────────────────
    async def async_batch_update(self, params_vo_list: list[dict]) -> Any:
        """agent_async_batch_update_advert → 返回含 taskId 的结果。"""
        return await self.call(
            "agent_async_batch_update_advert", {"paramsVoList": params_vo_list}
        )

    async def batch_update_result(self, task_ids: list[str]) -> Any:
        """agent_batch_update_advert_result → 异步任务逐项结果。"""
        return await self.call(
            "agent_batch_update_advert_result", {"taskIds": task_ids}
        )

    async def create_portfolio_campaign(self, payload: dict) -> Any:
        """agent_create_portfolio_campaign → 新建活动（可选含组合）。"""
        return await self.call("agent_create_portfolio_campaign", payload)

    async def create_negative_keywords(self, payload: dict) -> Any:
        """agent_create_negative_keywords → 否定词/关键词。"""
        return await self.call("agent_create_negative_keywords", payload)

    # ── 读工具（安全，不改广告）────────────────────────────────────────
    async def query_portfolio_list(
        self, shop_id: int, parent_asin: str, parent_seller_sku: str,
        portfolio_name_like: str = "", current_user_id: str = "",
    ) -> Any:
        # 该 MCP 工具按 currentUserId 用户隔离（不传可能查不全），故透传操作人 ID。
        args: dict[str, Any] = {
            "shopId": shop_id, "parentAsin": parent_asin,
            "parentSellerSku": parent_seller_sku,
        }
        if current_user_id:
            args["currentUserId"] = current_user_id
        if portfolio_name_like:
            args["portfolioNameLike"] = portfolio_name_like
        return await self.call("agent_query_portfolio_list", args)

    async def query_keyword_suggest_bid(
        self, shop_id: int, parent_asin: str, parent_seller_sku: str,
        keywords: list[str], current_user_id: str = "",
    ) -> Any:
        args: dict[str, Any] = {
            "shopId": shop_id, "parentAsin": parent_asin,
            "parentSellerSku": parent_seller_sku,
            "keywordVoList": [{"keyword": k} for k in keywords],
        }
        if current_user_id:
            args["currentUserId"] = current_user_id
        return await self.call("agent_query_keyword_suggest_bid", args)

    async def aclose(self) -> None:
        if self._invoker is not None:
            await self._invoker.aclose()
            self._invoker = None

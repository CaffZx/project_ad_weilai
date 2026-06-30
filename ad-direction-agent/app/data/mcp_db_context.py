"""MCP 上下文解析 — parent_listing_detail 工具获取 shop/sku/asin。"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from app.config.settings import settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class McpDbContext:
    parent_asin: str
    parent_seller_sku: str
    shop_account: str
    shop_id: int | None = None
    site_code: str = "Amazon_US"
    product_name: str = ""


class McpDbContextError(Exception):
    """Raised when listing context cannot be resolved."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def _coerce_int(v) -> int | None:
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


_MCP_PARENT_KEY_MAP: dict[str, str] = {
    "父ASIN": "parent_asin",
    "父卖家SKU": "parent_seller_sku",
    "店铺ID": "shop_id",
    "店铺账号": "shop_account",
    "站点": "site_code",
    "产品中文名": "product_cn_name",
    "产品名称": "product_name",
}


async def resolve_mcp_context_from_mcp(asin: str, adapter) -> McpDbContext | None:
    """通过 MCP parent_listing_detail 解析上下文（纯 MCP，不再回落 DB）。"""
    import json

    try:
        res = await adapter.call_tool_timed_with_args(
            "parent_listing_detail",
            {"parent_asin": asin},
            timeout=getattr(settings, "mcp_context_timeout", 30.0),
        )
        if not res.ok:
            logger.warning("resolve_mcp_context_from_mcp [%s] MCP 失败: %s", asin, res.error)
            return None
        raw = res.value
        if isinstance(raw, dict) and "content" in raw:
            for item in raw["content"]:
                txt = item.get("text", "")
                if isinstance(txt, str):
                    raw = json.loads(txt)
                    break
        if isinstance(raw, dict) and "success" in raw:
            rows = raw.get("rows") or []
            raw = rows[0] if rows else {}
        if not raw or not isinstance(raw, dict):
            return None
        mapped = {_MCP_PARENT_KEY_MAP.get(k, k): v for k, v in raw.items()}
        product_name = str(mapped.pop("product_cn_name", "") or mapped.get("product_name", ""))
        site_code = str(mapped.get("site_code") or settings.mcp_default_site_code or "Amazon_US")
        psku = str(mapped.get("parent_seller_sku") or "")
        shop = str(mapped.get("shop_account") or "")
        if not psku or not shop:
            logger.warning(
                "resolve_mcp_context_from_mcp [%s] 响应缺少必要字段 (sku=%r shop=%r)",
                asin, psku or "(空)", shop or "(空)",
            )
            return None
        return McpDbContext(
            parent_asin=str(mapped.get("parent_asin") or asin),
            parent_seller_sku=psku,
            shop_account=shop,
            shop_id=_coerce_int(mapped.get("shop_id")),
            site_code=site_code,
            product_name=product_name,
        )
    except Exception as e:
        logger.warning("resolve_mcp_context_from_mcp [%s] 异常: %s", asin, e)
        return None

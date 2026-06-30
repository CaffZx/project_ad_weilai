"""数据聚合调度器 — 分阶段 MCP + 单工具回落 Doris。"""

from __future__ import annotations

import asyncio
import logging

from app.config.settings import settings
from app.data.base import DataSourceAdapter
from app.data.mock import MockAdapter
from app.data.phased_fetcher import fetch_phased
from app.models.asin_data import ASINData

logger = logging.getLogger(__name__)


class DataAggregator:
    """数据聚合器"""

    def __init__(self, adapter: DataSourceAdapter | None = None):
        self.adapter = adapter or self._create_adapter()

    def _create_adapter(self) -> DataSourceAdapter:
        source = settings.data_source
        if source == "mock":
            return MockAdapter()
        if source == "mcp":
            from app.data.mcp_adapter import McpAdapter
            return McpAdapter()
        if source == "csv":
            from app.data.csv_adapter import CsvAdapter
            return CsvAdapter(
                data_dir=settings.data_source_dir,
                filename=getattr(settings, "csv_filename", None),
                key_column=getattr(settings, "csv_key_column", "asin"),
            )
        raise ValueError(f"未知的数据源类型: {source}")

    async def fetch(
        self,
        asin: str,
        meta_filter: list[str] | None = None,
        days: int = 7,
    ) -> ASINData:
        try:
            if settings.data_source == "mcp":
                if settings.skills_enabled:
                    from app.skills.registry import skill_registry

                    data = await skill_registry.run(
                        "mcp-query",
                        asin=asin,
                        meta_filter=meta_filter,
                        days=days,
                    )
                else:
                    data = await fetch_phased(
                        asin,
                        meta_filter=meta_filter,
                        days=days,
                    )
            else:
                data = await self.adapter.fetch_asin_data(
                    asin, meta_filter=meta_filter, days=days,
                )
            return data
        except Exception as e:
            logger.exception("DataAggregator fetch failed [%s]: %s", asin, e)
            return ASINData(
                asin=asin,
                data_missing=True,
                missing_fields=["all"],
                data_freshness="partial",
            )


data_aggregator = DataAggregator()

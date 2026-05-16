"""数据聚合调度器 — 根据标签组合判断需要哪些数据，调度适配器拉取"""

from app.config.settings import settings
from app.data.base import DataSourceAdapter
from app.data.mock import MockAdapter
from app.models.asin_data import ASINData


class DataAggregator:
    """数据聚合器

    职责:
    1. 根据 ASIN 和方向判断数据需求
    2. 调度 DataSourceAdapter 拉取数据
    3. 处理超时/缺失降级
    """

    def __init__(self, adapter: DataSourceAdapter | None = None):
        self.adapter = adapter or self._create_adapter()

    def _create_adapter(self) -> DataSourceAdapter:
        source = settings.data_source
        if source == "mock":
            return MockAdapter()
        if source == "db":
            from app.data.db_adapter import DbAdapter
            return DbAdapter()
        if source == "csv":
            from app.data.csv_adapter import CsvAdapter
            return CsvAdapter(
                data_dir=settings.data_source_dir,
                filename=getattr(settings, "csv_filename", None),
                key_column=getattr(settings, "csv_key_column", "asin"),
            )
        raise ValueError(f"未知的数据源类型: {source}")

    async def fetch(self, asin: str,
                     meta_filter: list[str] | None = None,
                     days: int = 7) -> ASINData:
        """获取 ASIN 数据

        meta_filter: 元脚本ID列表，只查询指定的数据维度。
                     None 表示查询全部（向后兼容）。
        days: 数据时间窗口（7/14/30），默认 7 天。
        """
        try:
            data = await self.adapter.fetch_asin_data(asin, meta_filter=meta_filter, days=days)
            return data
        except Exception as e:
            return ASINData(
                asin=asin,
                data_missing=True,
                missing_fields=["all"],
            )


# 全局单例
data_aggregator = DataAggregator()

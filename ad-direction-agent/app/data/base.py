from abc import ABC, abstractmethod
from typing import Optional

from app.models.asin_data import ASINData


class DataSourceAdapter(ABC):
    """数据源适配器抽象基类

    所有数据源（数仓、爬虫、Mock）都实现此接口。
    新增数据源 = 继承此类 + 实现全部 abstractmethod。
    """

    @abstractmethod
    async def fetch_asin_data(self, asin: str,
                               meta_filter: Optional[list[str]] = None,
                               days: int = 7) -> ASINData:
        """获取 ASIN 全量数据

        meta_filter: 元脚本ID列表，指定需要查询的数据维度。
                     None 表示查询全部。
        days: 数据时间窗口（7/14/30），默认 7 天。
        """
        ...

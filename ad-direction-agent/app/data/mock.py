"""Mock 数据适配器 — 严格按照数仓血缘（data_lineage）的字段口径生成

仅包含 DbAdapter._fetch_all() 实际从数仓查询并填充的字段，
不包含数仓未提供的计算字段（如 natural_order_ratio、tos_ratio、threat_score 等）。
"""

from datetime import datetime, timezone

from app.data.base import DataSourceAdapter
from app.models.asin_data import ASINData, AdData, KeywordData, CompetitorData, SpecialSignals


class MockAdapter(DataSourceAdapter):
    """Mock 数据源 — 字段口径与 DbAdapter 完全对齐"""

    async def fetch_asin_data(self, asin: str,
                               meta_filter: list[str] | None = None,
                               days: int = 7) -> ASINData:
        """Mock 始终返回全部数据，meta_filter 和 days 仅用于接口兼容"""
        scenarios = {
            "0XSTANDARD": self._scenario_standard(),
            "0XHIGHACOS": self._scenario_high_acos(),
            "0XSTABLE": self._scenario_stable(),
            "0XMISSING": self._scenario_missing(),
        }
        data = scenarios.get(asin.upper(), self._scenario_not_found(asin))
        data.asin = asin
        return data

    def _build_dates(self, days_ago: int):
        """模拟 product_site_launch_date → days_since_launch"""
        return (datetime.now(timezone.utc).date() - __import__("datetime").timedelta(days=days_ago)).isoformat()

    # ── 场景1: 标准品（适合推进自然位）────────────────────

    def _scenario_standard(self) -> ASINData:
        """product_stage=进展期, season_stage=旺季准备, ACOS=22%, 有上升词"""
        data = ASINData(asin="0XSTANDARD")
        data.sku = "FS0XSTD-US"
        data.parent_asin = "0XSTANDARD"
        data.price = 29.99
        data.margin = 0.35
        data.avg_daily_sales_30d = 15.2
        data.natural_order_ratio = None  # 数仓暂无此字段
        data.rating = 4.3
        data.review_count = 35
        data.product_level = "重点产品"
        data.product_stage = "推进期"
        data.season_stage = "旺季准备"
        data.brand = "TestBrand"
        data.product_line = "Dresses"
        data.category_name = "Women's Fashion"
        data.days_since_launch = 87
        data.refund_rate = 0.03
        data.profit_per_unit = 10.50
        data.keyword_count = 7
        data.available_new_keywords = 3

        data.ad_data = AdData(
            acos=22.0,
            cpc=0.66,
            ctr=0.45,
            cvr=10.2,
            impressions=28500,
            clicks=1280,
            spend=840.0,
            orders=131,
            daily_budget=60.0,
            tacos=15.5,
            daily_ad_spend_ratio=18.2,
            precision_acos=18.5,
            broad_acos=28.0,
            precision_cpc=0.72,
            broad_cpc=0.55,
            precision_spend_ratio=55.0,
            broad_spend_ratio=45.0,
        )
        data.natural_order_ratio = 42.0

        data.keywords = [
            KeywordData(keyword="summer dress", impressions=5200, clicks=312, spend=187.0, orders=18, acos=15.8, cvr=5.8, bid=0.85, natural_rank=12, rank_change_14d=5, match_type="EXACT"),
            KeywordData(keyword="floral dress", impressions=3800, clicks=228, spend=159.0, orders=15, acos=18.2, cvr=6.6, bid=0.92, natural_rank=18, rank_change_14d=8, match_type="EXACT"),
            KeywordData(keyword="cotton dress", impressions=2900, clicks=174, spend=104.0, orders=12, acos=22.0, cvr=6.9, bid=0.70, natural_rank=22, rank_change_14d=3, match_type="BROAD"),
            KeywordData(keyword="boho dress", impressions=1800, clicks=90, spend=63.0, orders=5, acos=25.0, cvr=5.6, bid=0.60, natural_rank=35, rank_change_14d=2, match_type="BROAD"),
            KeywordData(keyword="casual summer dress", impressions=1200, clicks=72, spend=43.0, orders=8, acos=18.5, cvr=11.1, bid=0.55, natural_rank=28, rank_change_14d=6, match_type="EXACT"),
            KeywordData(keyword="party dress", impressions=2500, clicks=125, spend=100.0, orders=6, acos=28.0, cvr=4.8, bid=0.80, natural_rank=40, rank_change_14d=-2, match_type="BROAD"),
            KeywordData(keyword="maxi dress", impressions=900, clicks=45, spend=31.0, orders=3, acos=35.0, cvr=6.7, bid=0.50, natural_rank=50, rank_change_14d=1, match_type="PHRASE"),
        ]

        data.competitors = [
            CompetitorData(asin="0XCOMP01", price=31.99, rating=4.1, review_count=42, bsr=8500),
            CompetitorData(asin="0XCOMP02", price=27.99, rating=4.0, review_count=28, bsr=12000),
            CompetitorData(asin="0XCOMP03", price=34.99, rating=4.5, review_count=156, bsr=3200),
        ]

        data.signals = SpecialSignals(
            inventory_qty=480,
            in_transit_inventory=200,
        )

        return data

    # ── 场景2: 高 ACOS（适合优化 ACOS）────────────────────

    def _scenario_high_acos(self) -> ASINData:
        """product_stage=冲刺期, season_stage=大旺季, ACOS=42%, 高花费低效词"""
        data = ASINData(asin="0XHIGHACOS")
        data.sku = "FS0XHAC-US"
        data.parent_asin = "0XHIGHACOS"
        data.price = 24.99
        data.margin = 0.25
        data.avg_daily_sales_30d = 10.3
        data.natural_order_ratio = None
        data.rating = 3.9
        data.review_count = 18
        data.product_level = "重点产品"
        data.product_stage = "推进期"
        data.season_stage = "大旺季"
        data.brand = "TestBrand"
        data.product_line = "Dresses"
        data.category_name = "Women's Fashion"
        data.days_since_launch = 45
        data.refund_rate = 0.08
        data.profit_per_unit = 6.25
        data.keyword_count = 5
        data.available_new_keywords = 1

        data.ad_data = AdData(
            acos=42.0,
            cpc=1.25,
            ctr=0.32,
            cvr=6.8,
            impressions=32000,
            clicks=1024,
            spend=1280.0,
            orders=70,
            daily_budget=80.0,
            tacos=35.0,
            daily_ad_spend_ratio=25.0,
            precision_acos=48.0,
            broad_acos=55.0,
            precision_cpc=1.40,
            broad_cpc=1.10,
            precision_spend_ratio=60.0,
            broad_spend_ratio=40.0,
        )
        data.natural_order_ratio = 25.0

        data.keywords = [
            KeywordData(keyword="women dress", impressions=8500, clicks=340, spend=425.0, orders=8, acos=55.0, cvr=2.4, bid=1.50, natural_rank=45, rank_change_14d=0, match_type="BROAD"),
            KeywordData(keyword="summer outfit", impressions=6200, clicks=248, spend=310.0, orders=5, acos=62.0, cvr=2.0, bid=1.30, natural_rank=55, rank_change_14d=-3, match_type="BROAD"),
            KeywordData(keyword="trendy dress", impressions=4800, clicks=192, spend=240.0, orders=3, acos=80.0, cvr=1.6, bid=1.20, natural_rank=60, rank_change_14d=-5, match_type="BROAD"),
            KeywordData(keyword="cute dress", impressions=3500, clicks=140, spend=175.0, orders=0, acos=None, cvr=0.0, bid=1.10, natural_rank=70, rank_change_14d=0, match_type="EXACT"),
            KeywordData(keyword="dress for women", impressions=2800, clicks=84, spend=84.0, orders=0, acos=None, cvr=0.0, bid=0.90, natural_rank=65, rank_change_14d=2, match_type="EXACT"),
        ]

        data.competitors = [
            CompetitorData(asin="0XCOMP04", price=22.99, rating=3.8, review_count=12, bsr=18000),
            CompetitorData(asin="0XCOMP05", price=26.99, rating=4.2, review_count=45, bsr=9500),
        ]

        data.signals = SpecialSignals(
            inventory_qty=320,
            in_transit_inventory=150,
        )

        return data

    # ── 场景3: 稳定态（适合平衡维持）────────────────────

    def _scenario_stable(self) -> ASINData:
        """product_stage=达成期, season_stage=淡季, ACOS=15%, 指标健康"""
        data = ASINData(asin="0XSTABLE")
        data.sku = "FS0XSTB-US"
        data.parent_asin = "0XSTABLE"
        data.price = 34.99
        data.margin = 0.45
        data.avg_daily_sales_30d = 24.5
        data.natural_order_ratio = None
        data.rating = 4.5
        data.review_count = 128
        data.product_level = "战略级产品"
        data.product_stage = "收割利润期"
        data.season_stage = "淡季"
        data.brand = "TestBrand"
        data.product_line = "Dresses"
        data.category_name = "Women's Fashion"
        data.days_since_launch = 180
        data.refund_rate = 0.02
        data.profit_per_unit = 15.75
        data.keyword_count = 4
        data.available_new_keywords = 0

        data.ad_data = AdData(
            acos=15.0,
            cpc=0.44,
            ctr=0.52,
            cvr=12.0,
            impressions=22000,
            clicks=1144,
            spend=500.0,
            orders=137,
            daily_budget=40.0,
            tacos=10.0,
            daily_ad_spend_ratio=12.0,
            precision_acos=12.0,
            broad_acos=20.0,
            precision_cpc=0.48,
            broad_cpc=0.38,
            precision_spend_ratio=45.0,
            broad_spend_ratio=55.0,
        )
        data.natural_order_ratio = 60.0

        data.keywords = [
            KeywordData(keyword="premium dress", impressions=3000, clicks=180, spend=90.0, orders=15, acos=10.0, cvr=8.3, bid=0.55, natural_rank=5, rank_change_14d=1, match_type="EXACT"),
            KeywordData(keyword="vintage dress", impressions=2500, clicks=150, spend=75.0, orders=12, acos=11.0, cvr=8.0, bid=0.50, natural_rank=8, rank_change_14d=0, match_type="EXACT"),
            KeywordData(keyword="elegant dress", impressions=2200, clicks=132, spend=66.0, orders=10, acos=13.0, cvr=7.6, bid=0.52, natural_rank=10, rank_change_14d=-1, match_type="BROAD"),
            KeywordData(keyword="formal dress", impressions=1800, clicks=108, spend=54.0, orders=8, acos=12.0, cvr=7.4, bid=0.48, natural_rank=12, rank_change_14d=2, match_type="EXACT"),
        ]

        data.competitors = [
            CompetitorData(asin="0XCOMP06", price=36.99, rating=4.3, review_count=98, bsr=4500),
            CompetitorData(asin="0XCOMP07", price=32.99, rating=4.1, review_count=72, bsr=6200),
        ]

        data.signals = SpecialSignals(
            inventory_qty=1850,
            in_transit_inventory=500,
        )

        return data

    # ── 场景4: 数据缺失 ────────────────────

    def _scenario_missing(self) -> ASINData:
        """大部分字段为空，模拟数据不完整的 ASIN"""
        data = ASINData(asin="0XMISSING")
        data.sku = "FS0XMIS-US"
        data.parent_asin = "0XMISSING"
        data.price = 19.99
        data.avg_daily_sales_30d = 2.0
        data.natural_order_ratio = None
        data.rating = 4.0
        data.review_count = 5
        data.product_level = "长尾产品"
        data.product_stage = "测试期"
        data.season_stage = "淡季"
        data.days_since_launch = 10

        data.ad_data = AdData(
            acos=None,
            daily_budget=20.0,
        )

        data.signals = SpecialSignals(
            inventory_qty=500,
        )

        data.data_missing = True
        data.missing_fields = ["acos", "keywords", "competitors", "brand", "product_line", "category_name", "ad_keywords"]

        return data

    # ── 未知 ASIN ──

    def _scenario_not_found(self, asin: str) -> ASINData:
        return ASINData(
            asin=asin,
            data_missing=True,
            missing_fields=["asin_not_found", "ad_data", "keywords", "competitors"],
        )

"""SQL 元脚本注册中心 — 7个元脚本的参数签名与用途定义

数据源对照:
  meta_01_keyword_ad.sql        ← 广告关键词.sql
  meta_02_competitor.sql        ← 直接竞品列表.sql
  meta_03a_kw_competitor_rank   ← DWD关键词竞品和子ASIN → 查询1
  meta_03b_kw_sub_asin_rank     ← DWD关键词竞品和子ASIN → 查询2
  meta_04_flow_keyword.sql      ← DWD在线列表流量关键词.sql
  meta_05a_ad_product_report    ← dwd广告报告查询sql → 查询1
  meta_05b_ad_placement_report  ← dwd广告报告查询sql → 查询2
  meta_05c_ad_search_term       ← dwd广告报告查询sql → 查询3
"""

from dataclasses import dataclass, field
from typing import Literal


ParamSource = Literal["user_input", "ai_decides", "system"]


@dataclass
class ParamDef:
    """元脚本参数定义"""
    name: str
    type: str  # "string" | "int" | "datetime"
    description: str
    source: ParamSource
    default_value: str | None = None


@dataclass
class MetaScriptDef:
    """元脚本定义"""
    meta_id: str
    file_name: str
    description: str
    source_file: str  # 原始SQL文件
    parameters: list[ParamDef] = field(default_factory=list)
    related_method: str = ""  # DbAdapter 中的对应方法名


# ── 注册中心 ────────────────────────────────────────────

_scripts: dict[str, MetaScriptDef] = {}


def register(defn: MetaScriptDef):
    _scripts[defn.meta_id] = defn


def get(meta_id: str) -> MetaScriptDef | None:
    return _scripts.get(meta_id)


def list_all() -> list[MetaScriptDef]:
    return list(_scripts.values())


def get_by_source(source_file: str) -> list[MetaScriptDef]:
    return [s for s in _scripts.values() if s.source_file == source_file]


# ── 注册 7 个元脚本 ────────────────────────────────────

register(MetaScriptDef(
    meta_id="META_KW_AD",
    file_name="meta_01_keyword_ad.sql",
    description="关键词级广告表现（花费/销售额/ACOS/CPC/单量），按 matchType 筛选",
    source_file="广告关键词.sql",
    related_method="_fetch_ad_keywords",
    parameters=[
        ParamDef("shopAccount", "string", "店铺账号", "user_input", ""),
        ParamDef("parentAsin", "string", "父ASIN", "user_input", "<parent_asin>"),
        ParamDef("parentSellerSku", "string", "父SKU", "system"),
        ParamDef("matchType", "string", "匹配类型(EXACT/BROAD/PHRASE)", "ai_decides", "EXACT"),
    ],
))

register(MetaScriptDef(
    meta_id="META_COMPETITOR",
    file_name="meta_02_competitor.sql",
    description="直接竞品 ASIN 信息（价格/评分/排名/品牌/变体）",
    source_file="直接竞品列表.sql",
    related_method="_fetch_competitors",
    parameters=[
        ParamDef("shopAccount", "string", "店铺账号", "user_input", ""),
        ParamDef("parentAsin", "string", "父ASIN", "user_input", "<parent_asin>"),
        ParamDef("parentSellerSku", "string", "父SKU", "system"),
    ],
))

register(MetaScriptDef(
    meta_id="META_KW_COMPETITOR_RANK",
    file_name="meta_03a_kw_competitor_rank.sql",
    description="指定关键词下各竞品 ASIN 的自然排名/广告排名",
    source_file="DWD关键词竞品和子ASIN的SQL.sql",
    related_method="_fetch_natural_rankings",
    parameters=[
        ParamDef("parent_asin", "string", "父ASIN", "user_input", "<parent_asin>"),
        ParamDef("parent_seller_sku", "string", "父SKU", "system"),
        ParamDef("shop_account", "string", "店铺账号", "user_input", ""),
        ParamDef("keyword", "string", "目标关键词", "ai_decides", "fishnet stockings for women"),
        ParamDef("siteCode", "string", "站点代码", "system", "Amazon_US"),
    ],
))

register(MetaScriptDef(
    meta_id="META_KW_SUB_ASIN_RANK",
    file_name="meta_03b_kw_sub_asin_rank.sql",
    description="指定关键词下本店子 ASIN 的自然排名及变化趋势",
    source_file="DWD关键词竞品和子ASIN的SQL.sql",
    related_method="_fetch_natural_rankings",
    parameters=[
        ParamDef("parent_asin", "string", "父ASIN", "user_input", "<parent_asin>"),
        ParamDef("parent_seller_sku", "string", "父SKU", "system"),
        ParamDef("shop_account", "string", "店铺账号", "user_input", ""),
        ParamDef("keyword", "string", "目标关键词", "ai_decides", "fishnet stockings for women"),
        ParamDef("siteCode", "string", "站点代码", "system", "Amazon_US"),
    ],
))

register(MetaScriptDef(
    meta_id="META_FLOW_KEYWORD",
    file_name="meta_04_flow_keyword.sql",
    description="Listing 关联的流量关键词及搜索量（切换表名后缀切换站点）",
    source_file="DWD在线列表流量关键词.sql",
    related_method="_fetch_flow_keywords",
    parameters=[
        ParamDef("parent_asin", "string", "父ASIN", "user_input", "<parent_asin>"),
        ParamDef("parent_seller_sku", "string", "父SKU", "system"),
        ParamDef("shop_account", "string", "店铺账号", "user_input", ""),
    ],
))

register(MetaScriptDef(
    meta_id="META_AD_PRODUCT",
    file_name="meta_05a_ad_product_report.sql",
    description="ASIN 级广告汇总：CTR/CPC/CVR/ACOS/花费/销售额",
    source_file="dwd广告报告查询sql.sql",
    related_method="_fetch_ad_summary",
    parameters=[
        ParamDef("parent_asin", "string", "父ASIN", "user_input", "<parent_asin>"),
        ParamDef("parent_seller_sku", "string", "父SKU", "system"),
        ParamDef("shop_account", "string", "店铺账号", "user_input", ""),
        ParamDef("startDate", "datetime", "开始时间", "system"),
        ParamDef("endDate", "datetime", "结束时间", "system"),
    ],
))

register(MetaScriptDef(
    meta_id="META_AD_PLACEMENT",
    file_name="meta_05b_ad_placement_report.sql",
    description="精准(TOS) vs 非精准(ROS) 广告位分位指标",
    source_file="dwd广告报告查询sql.sql",
    related_method="_fetch_placement_summary",
    parameters=[
        ParamDef("parent_asin", "string", "父ASIN", "user_input", "<parent_asin>"),
        ParamDef("parent_seller_sku", "string", "父SKU", "system"),
        ParamDef("shop_account", "string", "店铺账号", "user_input", ""),
        ParamDef("startDate", "datetime", "开始时间", "system"),
        ParamDef("endDate", "datetime", "结束时间", "system"),
    ],
))

register(MetaScriptDef(
    meta_id="META_AD_SEARCH_TERM",
    file_name="meta_05c_ad_search_term.sql",
    description="搜索词级广告表现（识别低效词/高转化词）",
    source_file="dwd广告报告查询sql.sql",
    related_method="",  # DbAdapter 暂无对应方法
    parameters=[
        ParamDef("parent_asin", "string", "父ASIN", "user_input", "<parent_asin>"),
        ParamDef("parent_seller_sku", "string", "父SKU", "system"),
        ParamDef("shop_account", "string", "店铺账号", "user_input", ""),
        ParamDef("startDate", "datetime", "开始时间", "system"),
        ParamDef("endDate", "datetime", "结束时间", "system"),
    ],
))

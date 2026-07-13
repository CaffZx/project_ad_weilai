"""Per-module data contracts and LLM completeness gate (pragmatic tier)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from app.models.asin_data import ASINData

ModuleId = Literal["tactics", "execution", "p3", "report", "campaign"]

Status = Literal["ok", "degraded", "blocked"]

# Pragmatic contracts: required missing → block LLM; important → feed with notice
# 2026-06-30: keywords 从 required 移除 — ad_keyword_report MCP 工具已下线、
# Doris 回落已切除，关键词级广告效果数据暂缺。目标关键词推荐后续用
# ad_optimization + ad_campaign_product_keyword_list 独立恢复。
DEFAULT_MODULE_CONTRACTS: dict[str, dict[str, list[str]]] = {
    "tactics": {
        "required": ["ad_data.acos", "ad_data.cpc"],
        "important": ["trend", "ad_data.ctr", "natural_order_ratio", "keywords"],
        "optional": ["competitor_price", "inventory_qty"],
    },
    "execution": {
        "required": ["ad_data.acos", "ad_data.cvr"],
        "important": ["margin", "trend", "ad_data.tacos", "natural_order_ratio", "keywords"],
        "optional": ["placement_comparison", "competitor_summary"],
    },
    "p3": {
        "required": ["ad_data.acos", "ad_data.spend", "ad_data.cpc"],
        "important": ["margin", "trend", "keywords", "ad_data.tacos"],
        "optional": ["competitor_summary", "history"],
    },
    "report": {
        "required": ["ad_data.acos"],
        "important": ["margin", "trend", "ad_data.cvr", "keywords"],
        "optional": ["placement_comparison", "competitor_summary"],
    },
    "campaign": {
        "required": [],
        "important": ["margin", "natural_order_ratio", "inventory_qty", "avg_daily_sales_30d"],
        "optional": ["placement_comparison"],
    },
}

_FIELD_LABELS: dict[str, str] = {
    "keywords": "关键词列表",
    "ad_data.acos": "ACOS",
    "ad_data.cvr": "CVR",
    "ad_data.cpc": "CPC",
    "ad_data.spend": "广告花费",
    "ad_data.ctr": "CTR",
    "ad_data.tacos": "TACOS",
    "margin": "毛利率",
    "trend": "日趋势",
    "natural_order_ratio": "自然单占比",
    "competitor_price": "竞品价格",
    "inventory_qty": "可售库存",
    "placement_comparison": "广告位对比",
    "competitor_summary": "竞品摘要",
    "history": "历史调整记录",
    "campaigns": "广告活动列表",
    "avg_daily_sales_30d": "日均销量",
}


@dataclass
class CompletenessVerdict:
    status: Status
    missing_required: list[str] = field(default_factory=list)
    missing_important: list[str] = field(default_factory=list)
    retryable: bool = False

    def to_completeness_dict(self) -> dict:
        return {
            "status": self.status,
            "missing_required": list(self.missing_required),
            "missing_important": list(self.missing_important),
            "retryable": self.retryable,
        }


def get_module_contract(module: ModuleId) -> dict[str, list[str]]:
    return DEFAULT_MODULE_CONTRACTS.get(module, DEFAULT_MODULE_CONTRACTS["execution"])


def _get_attr_path(data: ASINData, path: str) -> Any:
    if path == "keywords":
        return data.keywords
    if path == "history":
        return None  # workflow state, not on ASINData
    if path == "placement_comparison":
        ad = data.ad_data
        if ad and (ad.placement_tos_acos is not None or ad.placement_ros_acos is not None):
            return True
        return None
    if path == "competitor_summary":
        return data.competitors if data.competitors else None
    if path == "competitor_price":
        return data.competitor_price_p50
    if path == "inventory_qty":
        return data.signals.inventory_qty if data.signals else None
    if path == "margin":
        return data.margin
    if path == "trend":
        return data.trend
    if path == "natural_order_ratio":
        return data.natural_order_ratio
    if path == "avg_daily_sales_30d":
        return data.avg_daily_sales_30d
    if path == "campaigns":
        return None  # campaigns from CampaignData, not ASINData
    if path.startswith("ad_data."):
        ad = data.ad_data
        if not ad:
            return None
        return getattr(ad, path.split(".", 1)[1], None)
    return getattr(data, path, None)


def _is_present(path: str, value: Any) -> bool:
    if path == "keywords":
        return bool(value)
    if path == "trend":
        return bool(value)
    if path in ("placement_comparison", "competitor_summary"):
        return value is not None
    if path == "history":
        return True  # optional-only; never evaluated as required on ASINData
    if path == "campaigns":
        return True  # always present when this module is reached
    if path == "avg_daily_sales_30d":
        return value is not None
    if path in ("competitor_price", "inventory_qty", "margin", "natural_order_ratio"):
        return value is not None
    if path.startswith("ad_data."):
        if value is None:
            return False
        field_name = path.split(".", 1)[1]
        if field_name in ("acos", "cvr", "ctr", "tacos"):
            return value != 0
        if field_name == "spend":
            return value is not None
        return value is not None
    return value is not None


def _infer_retryable(data: ASINData) -> bool:
    mf = set(data.missing_fields or [])
    if "asin_not_found" in mf or "context" in mf:
        return False
    if data.data_missing and mf == {"all"}:
        return True
    pf = " ".join(data.partial_failures or []).lower()
    if any(x in pf for x in ("timeout", "fail", "error")):
        return True
    if data.data_missing:
        return bool(mf - {"asin_not_found", "context"})
    return False


def evaluate_completeness(module: ModuleId, data: ASINData) -> CompletenessVerdict:
    contract = get_module_contract(module)
    missing_required: list[str] = []
    missing_important: list[str] = []

    if data.data_missing:
        for path in contract["required"]:
            if path == "history":
                continue
            missing_required.append(path)
    else:
        for path in contract["required"]:
            if path == "history":
                continue
            if not _is_present(path, _get_attr_path(data, path)):
                missing_required.append(path)
        for path in contract["important"]:
            if path == "history":
                continue
            if not _is_present(path, _get_attr_path(data, path)):
                missing_important.append(path)

    retryable = _infer_retryable(data) if missing_required else False

    if missing_required:
        return CompletenessVerdict(
            status="blocked",
            missing_required=missing_required,
            missing_important=missing_important,
            retryable=retryable,
        )
    if missing_important or (data.partial_failures and data.data_freshness != "fresh"):
        return CompletenessVerdict(
            status="degraded",
            missing_required=[],
            missing_important=missing_important,
            retryable=False,
        )
    return CompletenessVerdict(status="ok")


def field_labels(paths: list[str]) -> list[str]:
    return [_FIELD_LABELS.get(p, p) for p in paths]


def build_missing_notice(verdict: CompletenessVerdict) -> str:
    if verdict.status == "ok":
        return ""
    parts: list[str] = []
    if verdict.missing_important:
        parts.append(
            "以下数据维度不完整或缺失："
            + "、".join(field_labels(verdict.missing_important))
            + "。"
        )
    parts.append(
        "请仅基于已有指标给出判断，并在 reasoning/confidence 中明确标注数据不完整、降低置信度。"
    )
    return " ".join(parts)


def blocked_message(verdict: CompletenessVerdict) -> str:
    labels = field_labels(verdict.missing_required)
    base = "核心数据不足，无法生成 AI 建议。缺失项：" + "、".join(labels)
    if verdict.retryable:
        base += "。已尝试重新拉取仍不完整，请稍后刷新或检查 MCP/数仓连接。"
    else:
        base += "。请核对 ASIN、店铺上下文或数据源配置。"
    return base


def blocked_payload(asin: str, verdict: CompletenessVerdict) -> dict:
    return {
        "status": "blocked",
        "llm_status": "blocked",
        "asin": asin,
        "message": blocked_message(verdict),
        "missing_required": verdict.missing_required,
        "missing_important": verdict.missing_important,
        "missing_required_labels": field_labels(verdict.missing_required),
        "retryable": verdict.retryable,
        "data_completeness": verdict.to_completeness_dict(),
    }


def merge_completeness_into_summary(summary: dict, verdict: CompletenessVerdict) -> dict:
    out = dict(summary)
    out["data_completeness"] = verdict.status
    out["completeness"] = verdict.to_completeness_dict()
    notice = build_missing_notice(verdict)
    if notice:
        out["missing_notice"] = notice
    return out


def evaluate_campaign_completeness(
    campaign_count: int,
    asin_data: ASINData,
) -> CompletenessVerdict:
    """Campaign 专用完整性校验：活动数量 + ASIN 级关键字段。"""
    if campaign_count == 0:
        return CompletenessVerdict(
            status="degraded",
            missing_important=["campaigns"],
            retryable=False,
        )
    contract = get_module_contract("campaign")
    missing_important: list[str] = []
    for path in contract["important"]:
        if not _is_present(path, _get_attr_path(asin_data, path)):
            missing_important.append(path)
    if missing_important:
        return CompletenessVerdict(
            status="degraded",
            missing_important=missing_important,
            retryable=False,
        )
    return CompletenessVerdict(status="ok")

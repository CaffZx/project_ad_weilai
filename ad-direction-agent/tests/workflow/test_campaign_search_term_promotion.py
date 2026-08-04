"""广泛搜索词提精准入的契约与策略测试。"""

import json
import asyncio

import pytest

from app.llm.reasoner import (
    LLMReasoner,
    _build_campaign_system_prompt,
    _build_new_campaign_prompt,
)
from app.models.campaign import NewCampaignDecision, SearchTermPromotionCandidate
from app.workflow.steps.campaign_search_term_promotion import (
    build_search_term_promotion_decisions,
)
from app.workflow.steps.campaign_new import (
    finalize_new_campaign_decisions,
    merge_new_campaign_decisions,
)
from app.workflow.steps.campaign import _run_round


def test_broad_prompt_exposes_positive_search_term_candidate_contract():
    prompt = _build_campaign_system_prompt("broad")

    assert '"exact_promotion_candidates"' in prompt
    assert '"cid"' in prompt
    assert '"search_term"' in prompt
    assert '"keyword_root"' in prompt
    assert '"relevance_tier"' in prompt


def test_broad_prompt_uses_current_search_term_window_contract():
    prompt = _build_campaign_system_prompt("broad")

    assert "当前仅注入 7 天搜索词数据，不含更长时间窗口" not in prompt
    assert "搜索词点击<10 且花费<$5" not in prompt
    assert "每轮必读搜索词报告" not in prompt
    assert "max($15, 目标 CPA)" in prompt
    assert "7d 或 14d" in prompt
    assert "无逐词搜索词数据" in prompt


def test_exact_prompt_does_not_request_search_term_promotions():
    prompt = _build_campaign_system_prompt("exact")

    assert '"exact_promotion_candidates"' not in prompt


def test_source_a_prompt_does_not_imply_long_tail_means_exact():
    prompt = _build_new_campaign_prompt()

    assert "优先精准长尾词" not in prompt
    assert "新建精准活动承接" not in prompt
    assert "source=flow" in prompt
    assert "固定组装为 BROAD" in prompt
    assert "negative_strategy 必须填写非空" in prompt
    assert "点击>10次且无转化" not in prompt


class _FakeClient:
    async def chat(self, **_kwargs):
        return json.dumps({
            "campaign_adjustments": [],
            "exact_promotion_candidates": [
                {
                    "cid": "C1",
                    "search_term": "sticky bra",
                    "keyword_root": "sticky bra",
                    "keyword_class": "long_tail",
                    "relevance_tier": "R1",
                    "reason": "直接相关",
                    "evidence": ["模型候选"],
                },
                {
                    "cid": "C1",
                    "search_term": "invented term",
                    "keyword_root": "invented term",
                    "keyword_class": "long_tail",
                    "relevance_tier": "R1",
                    "reason": "幻觉词",
                    "evidence": [],
                },
            ],
            "batch_summary": {},
        })


class _NegativeClient:
    async def chat(self, **_kwargs):
        return json.dumps({
            "campaign_adjustments": [{
                "cid": "C1",
                "action": "adjust_bid",
                "direction": {"bid": "down", "budget": "keep"},
                "triggered_rule": "KEYWORD_POOL_DIRTY",
                "reason": "清理无关搜索词",
                "proposed_budget": 10.0,
                "proposed_bid": 0.5,
                "evidence": ["7d 搜索词事实"],
                "negative_keywords": [
                    {
                        "keyword": "real term",
                        "match_type": "NEGATIVE_EXACT",
                        "reason": "7d 明显不相关",
                    },
                    {
                        "keyword": "invented term",
                        "match_type": "NEGATIVE_EXACT",
                        "reason": "幻觉词",
                    },
                ],
                "review_level": "MANUAL_REVIEW",
            }],
            "exact_promotion_candidates": [],
            "batch_summary": {},
        })


@pytest.mark.asyncio
async def test_broad_parser_keeps_only_report_terms_and_backfills_authoritative_metrics():
    reasoner = LLMReasoner(client=_FakeClient())
    result = await reasoner.recommend_campaign_batch(
        asin="B0PARENT",
        campaign_summaries=[{
            "campaign_key": "camp-key",
            "campaign_name": "broad-campaign",
            "match_type": "BROAD",
            "keyword_text": "seed keyword",
            "_search_term_data": [{
                "keyword": "sticky bra",
                "clicks": 8,
                "orders": 3,
                "cost": 12.5,
                "sales": 50.0,
            }],
        }],
        strategy_context={},
        task_type="broad",
    )

    assert result["success"] is True
    assert result["parsed"]["exact_promotion_candidates"] == [{
        "campaign_key": "camp-key",
        "campaign_name": "broad-campaign",
        "campaign_match_type": "BROAD",
        "search_term": "sticky bra",
        "keyword_root": "sticky bra",
        "keyword_class": "long_tail",
        "relevance_tier": "R1",
        "reason": "直接相关",
        "evidence": ["模型候选"],
        "clicks": 8,
        "orders": 3,
        "cost": 12.5,
        "sales": 50.0,
    }]


@pytest.mark.asyncio
async def test_broad_parser_keeps_only_selected_7d_report_terms_for_negatives():
    reasoner = LLMReasoner(client=_NegativeClient())
    result = await reasoner.recommend_campaign_batch(
        asin="B0PARENT",
        campaign_summaries=[{
            "campaign_key": "camp-key",
            "campaign_name": "broad-campaign",
            "match_type": "BROAD",
            "keyword_text": "seed keyword",
            "_search_term_data": {
                "terms": [{
                    "keyword": "Real   Term",
                    "metrics_7d": {"clicks": 12, "cost": 20.0},
                }],
            },
        }],
        strategy_context={},
        task_type="broad",
    )

    negatives = result["parsed"]["campaign_adjustments"][0]["negative_keywords"]
    assert negatives == [{
        "keyword": "Real   Term",
        "match_type": "NEGATIVE_EXACT",
        "reason": "7d 明显不相关",
    }]


@pytest.mark.asyncio
async def test_broad_parser_blocks_negatives_when_search_term_fetch_was_skipped():
    reasoner = LLMReasoner(client=_NegativeClient())
    result = await reasoner.recommend_campaign_batch(
        asin="B0PARENT",
        campaign_summaries=[{
            "campaign_key": "camp-key",
            "campaign_name": "broad-campaign",
            "match_type": "BROAD",
            "keyword_text": "seed keyword",
            "search_term_fetch_status": "SKIPPED_CAMPAIGN_SAMPLE_INSUFFICIENT",
        }],
        strategy_context={},
        task_type="broad",
    )

    adjustment = result["parsed"]["campaign_adjustments"][0]
    assert adjustment["negative_keywords"] == []


def _candidate(
    term: str,
    *,
    campaign_key: str = "camp-1",
    root: str = "",
    orders: int = 0,
    cost: float = 0.0,
    sales: float = 0.0,
    relevance: str = "R1",
) -> SearchTermPromotionCandidate:
    return SearchTermPromotionCandidate(
        campaign_key=campaign_key,
        campaign_name=campaign_key,
        campaign_match_type="BROAD",
        search_term=term,
        keyword_root=root,
        keyword_class="long_tail",
        relevance_tier=relevance,
        reason="相关",
        evidence=["模型语义判断"],
        clicks=10,
        orders=orders,
        cost=cost,
        sales=sales,
    )


def test_direct_search_term_signal_becomes_exact_decision():
    decisions, warnings = build_search_term_promotion_decisions(
        [_candidate("sticky bra", orders=3, cost=12, sales=60)],
        existing_exact_keywords=set(),
        target_acos=25,
    )

    assert warnings == []
    assert len(decisions) == 1
    decision = decisions[0]
    assert decision.keyword_text == "sticky bra"
    assert decision.prescribed_match_type == "EXACT"
    assert decision.trigger_scene == "KEYWORD_PROMOTED_FROM_BROAD"
    assert decision.source == "SRC_CONVERTED"
    assert decision.review_level == "MANUAL_REVIEW"
    assert "订单通道" in decision.evidence[0]


def test_existing_exact_blocks_direct_signal_but_existing_broad_does_not():
    candidate = _candidate("sticky bra", orders=3, cost=12, sales=60)

    blocked, _ = build_search_term_promotion_decisions(
        [candidate], existing_exact_keywords={"sticky bra"}, target_acos=25,
    )
    allowed, _ = build_search_term_promotion_decisions(
        [candidate], existing_exact_keywords={"different exact"}, target_acos=25,
    )

    assert blocked == []
    assert [item.keyword_text for item in allowed] == ["sticky bra"]


def test_root_signal_aggregates_real_terms_across_broad_campaigns():
    decisions, _ = build_search_term_promotion_decisions(
        [
            _candidate("red sticky bra", campaign_key="camp-1", root="sticky bra", orders=1, cost=5, sales=20),
            _candidate("black sticky bra", campaign_key="camp-2", root="sticky bra", orders=2, cost=6, sales=30),
        ],
        existing_exact_keywords=set(),
        target_acos=25,
    )

    assert [item.keyword_text for item in decisions] == ["sticky bra"]
    assert "词根通道" in decisions[0].evidence[0]


def test_r2_candidate_cannot_enter_exact_promotion():
    decisions, _ = build_search_term_promotion_decisions(
        [_candidate("sticky bra", orders=3, cost=12, sales=60, relevance="R2")],
        existing_exact_keywords=set(),
        target_acos=25,
    )

    assert decisions == []


@pytest.mark.asyncio
async def test_exact_promotion_reuses_new_campaign_name_child_asin_and_bid_builder():
    decisions, _ = build_search_term_promotion_decisions(
        [_candidate("sticky bra", orders=3, cost=12, sales=60)],
        existing_exact_keywords=set(),
        target_acos=25,
    )

    class Fetcher:
        async def fetch_suggested_bids(self, keywords, *_args):
            assert keywords == ["sticky bra"]
            return {"sticky bra": 0.8}

    items = await finalize_new_campaign_decisions(
        decisions,
        fetcher=Fetcher(),
        parent_asin="B0PARENT",
        parent_seller_sku="SKU",
        shop_account="shop",
        target_child_asin="B0CHILD",
    )

    assert len(items) == 1
    item = items[0]
    assert item.keyword_text == "sticky bra"
    assert item.match_type == "EXACT"
    assert item.campaign_type == "精准广告"
    assert item.child_asin == "B0CHILD"
    assert item.campaign_name.startswith("精准-sticky bra-")
    assert item.proposed_base_bid == 0.4


@pytest.mark.asyncio
async def test_broad_round_carries_validated_promotion_candidates_to_orchestrator():
    candidate = _candidate("sticky bra", orders=3, cost=12, sales=60).model_dump()

    class Reasoner:
        async def recommend_campaign_batch(self, **_kwargs):
            return {
                "success": True,
                "parsed": {"campaign_adjustments": [], "exact_promotion_candidates": [candidate]},
                "raw_output": "{}",
            }

    results = await _run_round(
        Reasoner(), "B0PARENT", [[{"campaign_key": "camp-1"}]], {}, 0.0,
        asyncio.Semaphore(1), 1, task_type="broad",
    )

    assert [item.model_dump() for item in results[0].exact_promotion_candidates] == [candidate]


def test_search_term_exact_replaces_only_flow_decision_and_preserves_other_legacy_sources():
    def decision(keyword, source, match_type):
        return NewCampaignDecision(
            keyword_text=keyword,
            prescribed_match_type=match_type,
            source=source,
            trigger_scene="KEYWORD_PROMOTED_FROM_BROAD" if source == "SRC_CONVERTED" else "FLOW",
        )

    merged = merge_new_campaign_decisions(
        [
            decision("sticky bra", "flow", "BROAD"),
            decision("backless bra", "ranking_opportunity", "EXACT"),
        ],
        [
            decision("sticky bra", "SRC_CONVERTED", "EXACT"),
            decision("backless bra", "SRC_CONVERTED", "EXACT"),
        ],
    )

    by_keyword = {item.keyword_text: item for item in merged}
    assert by_keyword["sticky bra"].source == "SRC_CONVERTED"
    assert by_keyword["backless bra"].source == "ranking_opportunity"

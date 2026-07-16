import pytest
from fastapi.responses import JSONResponse

from app.config.settings import settings
from app.api import core_keyword as core_keyword_api
from app.data.core_keyword_fetcher import (
    CampaignKeywordItem,
    CoreKeywordFetcher,
    FetchedContext,
    FetchResult,
    ListingProductInfo,
)
from app.llm.reasoner import LLMReasoner
from app.models.campaign import CampaignAdjustmentItem, CampaignPerf, CampaignUnit
from app.persistence.erp_writer.repository import (
    ErpDualWriterRepository,
    core_keyword_task_version,
    resolve_effective_core_keywords,
)
from app.workflow.steps import campaign as campaign_step
from app.workflow.steps import core_keyword as CK
from app import start_core_keyword_server


class _CallResult:
    def __init__(self, value=None, *, ok=True, error=""):
        self.ok = ok
        self.value = value if value is not None else []
        self.error = error


class _FakeStarrocks:
    def __init__(self, payloads):
        self.payloads = payloads
        self.calls: list[tuple[str, dict]] = []

    async def call_tool_timed_with_args(self, tool_name, arguments, timeout):
        self.calls.append((tool_name, arguments))
        value = self.payloads.get(tool_name, [])
        return _CallResult(value)


class _FakeAzlisting:
    def __init__(self, payload):
        self.payload = payload
        self.calls: list[tuple[str, dict]] = []

    async def call_tool(self, tool_name, arguments):
        self.calls.append((tool_name, arguments))
        return self.payload


def test_resolve_effective_core_keywords_applies_policy_with_normalized_matching():
    result = resolve_effective_core_keywords(
        {"AI Dress", "disabled kw"},
        {
            " locked kw ": "LOCKED",
            "DISABLED KW": "DISABLED",
            "ai dress": "VETOED",
        },
    )

    assert result == {"locked kw"}


def test_core_keyword_task_version_matches_api_iso_datetime():
    assert core_keyword_task_version("2026-07-16T07:00:00.123456") == "2026-07-16T07:00:00.123456"
    assert core_keyword_task_version("2026-07-16 07:00:00.123456") == "2026-07-16T07:00:00.123456"


def test_new_core_keyword_task_id_is_unique_for_each_run():
    first = CK._new_task_id()
    second = CK._new_task_id()

    assert first != second
    assert first.startswith("ckt")
    assert len(first) == 32


def test_core_keyword_server_manual_entry_uses_project_root_as_workdir():
    assert start_core_keyword_server.SCRIPT_DIR == str(start_core_keyword_server.PROJECT_ROOT)


@pytest.mark.asyncio
async def test_fetcher_prefilters_locked_and_vetoed_before_perf_and_rank():
    fetcher = CoreKeywordFetcher()
    fake_starrocks = _FakeStarrocks({
        "parent_listing_detail": [{
            "shop_account": "shop_us",
            "parent_seller_sku": "SKU-1",
            "shop_id": 1622,
            "site_code": "Amazon_US",
        }],
        "ad_campaign_product_keyword_list": [
            {"campaign_id": "1", "广告活动名称": "camp_locked", "关键词": "locked kw", "关键词匹配类型": "EXACT"},
            {"campaign_id": "2", "广告活动名称": "camp_vetoed", "关键词": "vetoed kw", "关键词匹配类型": "EXACT"},
            {"campaign_id": "3", "广告活动名称": "camp_enabled", "关键词": "enabled kw", "关键词匹配类型": "EXACT"},
        ],
        "ad_campaign_product_report": [{"花费": 10, "广告订单量": 1, "ACOS": 25}],
        "keyword_child_asins": [{"keyword": "enabled kw", "craw_nature_rank": 12}],
        "flow_keywords": [],
    })
    fetcher._starrocks = fake_starrocks
    fetcher._azlisting = _FakeAzlisting([])

    result = await fetcher.fetch(
        "B0TEST", "SKU-1", 1622,
        excluded_keyword_norms={"locked kw", "vetoed kw"},
    )

    assert [item.keyword_text for item in result.campaigns] == ["enabled kw"]
    report_campaigns = [
        args["campaign_name"] for name, args in fake_starrocks.calls
        if name == "ad_campaign_product_report"
    ]
    ranking_keywords = [
        args["keyword"] for name, args in fake_starrocks.calls
        if name == "keyword_child_asins"
    ]
    assert report_campaigns == ["camp_enabled"]
    assert ranking_keywords == ["enabled kw"]


@pytest.mark.asyncio
async def test_step3_filters_multi_keyword_campaigns_before_data_core():
    fetcher = CoreKeywordFetcher()
    fetcher._starrocks = _FakeStarrocks({
        "ad_campaign_product_keyword_list": [
            {"广告活动名称": "camp_multi", "关键词": "core dress", "关键词匹配类型": "EXACT", "子ASIN": "B0C1"},
            {"广告活动名称": "camp_multi", "关键词": "summer dress", "关键词匹配类型": "EXACT", "子ASIN": "B0C2"},
            {"广告活动名称": "camp_single", "关键词": "party dress", "关键词匹配类型": "EXACT", "子ASIN": "B0C3"},
        ],
    })

    items = await fetcher._step3_campaign_keywords("B0TEST", "SKU-1", "shop_us")

    assert [item.campaign_name for item in items] == ["camp_single"]
    assert [item.keyword_text for item in items] == ["party dress"]


def test_data_core_conditions_mark_single_keyword_core():
    items = [
        CampaignKeywordItem(
            campaign_name="camp_cost",
            keyword_text="cost kw",
            match_type="EXACT",
            child_asin="B0C1",
            cost_14d=100.0,
        ),
        CampaignKeywordItem(
            campaign_name="camp_orders",
            keyword_text="orders kw",
            match_type="EXACT",
            child_asin="B0C2",
            cost_14d=10.0,
            orders_14d=3,
            acos_14d=30.0,
        ),
        CampaignKeywordItem(
            campaign_name="camp_rank",
            keyword_text="rank kw",
            match_type="EXACT",
            child_asin="B0C3",
            cost_14d=1.0,
            natural_rank=20,
            near_natural_rank=25,
        ),
        CampaignKeywordItem(
            campaign_name="camp_search",
            keyword_text="search kw",
            match_type="EXACT",
            child_asin="B0C4",
            cost_14d=0.5,
        ),
    ]

    result = CK._compute_data_core(
        items,
        flow_keywords=[
            {"keyword": "search kw", "searches": 1000},
            {"keyword": "other 1", "searches": 20},
            {"keyword": "other 2", "searches": 10},
        ],
        category="Dresses",
        target_acos=40.0,
    )

    assert result["cost kw"][0] is True
    assert result["orders kw"][0] is True
    assert result["rank kw"][0] is True
    assert result["search kw"][0] is True
    assert "高广告花费" in [e["condition"] for e in result["cost kw"][1]]
    assert "广告效果已验证" in [e["condition"] for e in result["orders kw"][1]]
    assert "自然排名价值" in [e["condition"] for e in result["rank kw"][1]]
    assert "高搜索量" in [e["condition"] for e in result["search kw"][1]]


def test_data_core_missing_orders_or_acos_does_not_crash():
    item = CampaignKeywordItem(
        campaign_name="camp-missing",
        keyword_text="missing perf kw",
        match_type="EXACT",
        child_asin="B0C1",
        cost_14d=1.0,
    )
    item.orders_14d = None
    item.acos_14d = None

    result = CK._compute_data_core(
        [item],
        flow_keywords=[],
        category="Dresses",
        target_acos=40.0,
    )

    assert result["missing perf kw"][0] is False


def test_semantic_conflict_blocks_data_core_in_merge():
    rows = CK._merge(
        ["blocked kw", "semantic kw"],
        [
            {
                "keyword_text": "blocked kw",
                "semantic_conflict": "fail",
                "conflict_reason": "品类不一致",
                "semantic_core": True,
                "semantic_evidence": ["x"],
            },
            {
                "keyword_text": "semantic kw",
                "semantic_conflict": "pass",
                "conflict_reason": "",
                "semantic_core": True,
                "semantic_evidence": ["x"],
            },
        ],
        {"blocked kw": (True, [{"condition": "高广告花费"}])},
    )

    by_kw = {row["keyword_text"]: row for row in rows}
    assert by_kw["blocked kw"]["is_core"] is False
    assert by_kw["blocked kw"]["data_core"] is True
    assert by_kw["semantic kw"]["is_core"] is True


def test_truncate_top_n_prefers_semantic_and_data_basis():
    rows = []
    for i in range(3):
        rows.append({
            "keyword_text": f"both {i}",
            "is_core": True,
            "core_basis": '["semantic_core", "data_core"]',
        })
    for i in range(3):
        rows.append({
            "keyword_text": f"data {i}",
            "is_core": True,
            "core_basis": '["data_core"]',
        })
    for i in range(3):
        rows.append({
            "keyword_text": f"semantic {i}",
            "is_core": True,
            "core_basis": '["semantic_core"]',
        })

    out = CK._truncate_top_n(rows, max_n=4)
    kept = [row["keyword_text"] for row in out if row["is_core"]]

    assert len(kept) == 4
    assert kept[:3] == ["both 0", "both 1", "both 2"]
    assert kept[3] == "data 0"


@pytest.mark.asyncio
async def test_fetcher_rejects_identity_mismatch_before_downstream_calls(monkeypatch):
    fetcher = CoreKeywordFetcher()
    fake_starrocks = _FakeStarrocks({
        "parent_listing_detail": [{
            "shop_account": "shop_us",
            "parent_seller_sku": "OTHER-SKU",
            "shop_id": 1622,
            "site_code": "Amazon_US",
        }]
    })
    fetcher._starrocks = fake_starrocks

    result = await fetcher.fetch("B0TEST", "SKU-1", 1622)

    assert "parent_seller_sku 不一致" in result.error
    assert [name for name, _ in fake_starrocks.calls] == ["parent_listing_detail"]


@pytest.mark.asyncio
async def test_fetcher_maps_live_parent_listing_detail_chinese_keys():
    fetcher = CoreKeywordFetcher()
    fetcher._starrocks = _FakeStarrocks({
        "parent_listing_detail": [{
            "店铺ID": 1622,
            "父ASIN": "B0TEST",
            "店铺账号": "am_test",
            "父卖家SKU": "SKU-1",
            "站点": "Amazon_US",
        }]
    })

    ctx = await fetcher._step1_context("B0TEST")

    assert ctx.shop_account == "am_test"
    assert ctx.parent_seller_sku == "SKU-1"
    assert ctx.shop_id == 1622
    assert ctx.site_code == "Amazon_US"


@pytest.mark.asyncio
async def test_fetcher_uses_azlisting_and_dedupes_campaign_reports(monkeypatch):
    old_url = settings.azlisting_mcp_url
    settings.azlisting_mcp_url = "http://azlisting.test/mcp"
    fetcher = CoreKeywordFetcher()
    fake_starrocks = _FakeStarrocks({
        "parent_listing_detail": [{
            "shop_account": "shop_us",
            "parent_seller_sku": "SKU-1",
            "shop_id": 1622,
            "site_code": "Amazon_US",
        }],
        "ad_campaign_product_keyword_list": [
            {"广告活动名称": "camp_multi", "关键词": "kw 1", "关键词匹配类型": "EXACT", "子ASIN": "B0C1"},
            {"广告活动名称": "camp_multi", "关键词": "kw 2", "关键词匹配类型": "EXACT", "子ASIN": "B0C2"},
            {"广告活动名称": "camp_single", "关键词": "kw 3", "关键词匹配类型": "EXACT", "子ASIN": "B0C3"},
        ],
        "ad_campaign_product_report": [{"花费": 12, "广告订单量": 2, "ACOS": 25}],
        "keyword_child_asins": [{"keyword": "kw", "craw_nature_rank": 12, "near_craw_nature_rank": 15}],
        "flow_keywords": [{"keyword": "kw 1", "searches": 100}],
    })
    fake_az = _FakeAzlisting([{
        "productName": "Test Dress",
        "fiveBulletPoint1": "Soft fabric",
        "variationThemeName": "ColorSize",
        "productColor": "Black",
        "productSize": "M",
        "lastCategory": '[{"title":"Dresses"}]',
    }])
    fetcher._starrocks = fake_starrocks
    fetcher._azlisting = fake_az

    try:
        result = await fetcher.fetch("B0TEST", "SKU-1", 1622)
    finally:
        settings.azlisting_mcp_url = old_url

    assert result.error == ""
    assert result.listing.title == "Test Dress"
    assert result.listing.category == "Dresses"
    assert fake_az.calls[0][0] == "erp_listing_product_info"
    report_calls = [args["campaign_name"] for name, args in fake_starrocks.calls if name == "ad_campaign_product_report"]
    assert report_calls == ["camp_single"]
    assert [item.keyword_text for item in result.campaigns] == ["kw 3"]


@pytest.mark.asyncio
async def test_fetcher_dedupes_campaign_keywords_before_ranking():
    fetcher = CoreKeywordFetcher()
    fake_starrocks = _FakeStarrocks({
        "parent_listing_detail": [{
            "shop_account": "shop_us",
            "parent_seller_sku": "SKU-1",
            "shop_id": 1622,
            "site_code": "Amazon_US",
        }],
        "ad_campaign_product_keyword_list": [
            {"campaign_name": "camp_one", "keyword": "repeat kw", "match_type": "EXACT", "child_asin": "B0C1"},
            {"campaign_name": "camp_two", "keyword": "repeat kw", "match_type": "EXACT", "child_asin": "B0C2"},
            {"campaign_name": "camp_three", "keyword": "unique kw", "match_type": "PHRASE", "child_asin": "B0C3"},
        ],
        "ad_campaign_product_report": [{"cost": 10, "orders": 2, "acos": 25}],
        "keyword_child_asins": [{"keyword": "repeat kw", "craw_nature_rank": 12, "near_craw_nature_rank": 15}],
        "flow_keywords": [],
    })
    fake_az = _FakeAzlisting([])
    fetcher._starrocks = fake_starrocks
    fetcher._azlisting = fake_az

    result = await fetcher.fetch("B0TEST", "SKU-1", 1622)

    assert result.error == ""
    assert [item.keyword_text for item in result.campaigns] == ["repeat kw", "unique kw"]
    assert result.campaigns[0].cost_14d == 20
    rank_calls = [
        args["keyword"]
        for name, args in fake_starrocks.calls
        if name == "keyword_child_asins"
    ]
    assert rank_calls == ["repeat kw", "unique kw"]


@pytest.mark.asyncio
async def test_fetcher_excludes_keywords_from_multi_keyword_campaigns():
    fetcher = CoreKeywordFetcher()
    fake_starrocks = _FakeStarrocks({
        "parent_listing_detail": [{
            "shop_account": "shop_us",
            "parent_seller_sku": "SKU-1",
            "shop_id": 1622,
            "site_code": "Amazon_US",
        }],
        "ad_campaign_product_keyword_list": [
            {"campaign_name": "camp_multi", "keyword": "multi kw one", "match_type": "EXACT", "child_asin": "B0C1"},
            {"campaign_name": "camp_multi", "keyword": "multi kw two", "match_type": "EXACT", "child_asin": "B0C2"},
            {"campaign_name": "camp_single", "keyword": "single kw", "match_type": "EXACT", "child_asin": "B0C3"},
        ],
        "ad_campaign_product_report": [{"cost": 10, "orders": 2, "acos": 25}],
        "keyword_child_asins": [{"keyword": "single kw", "craw_nature_rank": 12, "near_craw_nature_rank": 15}],
        "flow_keywords": [],
    })
    fetcher._starrocks = fake_starrocks
    fetcher._azlisting = _FakeAzlisting([])

    result = await fetcher.fetch("B0TEST", "SKU-1", 1622)

    assert result.error == ""
    assert [item.keyword_text for item in result.campaigns] == ["single kw"]
    report_calls = [
        args["campaign_name"]
        for name, args in fake_starrocks.calls
        if name == "ad_campaign_product_report"
    ]
    assert report_calls == ["camp_single"]
    rank_calls = [
        args["keyword"]
        for name, args in fake_starrocks.calls
        if name == "keyword_child_asins"
    ]
    assert rank_calls == ["single kw"]


@pytest.mark.asyncio
async def test_offline_analysis_uses_analyze_switch_batches_llm_and_caps_core_output(monkeypatch):
    old_analyze = settings.core_keyword_analyze_enabled
    old_consume = settings.core_keyword_enabled
    settings.core_keyword_analyze_enabled = True
    settings.core_keyword_enabled = False

    captured_batches: list[list[str]] = []

    class FakeFetcher:
        async def fetch(self, parent_asin, parent_seller_sku, shop_id, *, excluded_keyword_norms=None):
            campaigns = [
                CampaignKeywordItem(
                    campaign_name=f"camp_{i}",
                    keyword_text=f"keyword {i}",
                    match_type="EXACT",
                    child_asin=f"B0C{i:02d}",
                )
                for i in range(35)
            ]
            return FetchResult(
                context=FetchedContext(site_code="Amazon_US"),
                listing=ListingProductInfo(title="Test Dress", category="Dresses"),
                campaigns=campaigns,
                flow_keywords=[],
            )

    class FakeReasoner:
        async def recommend_semantic_core(
            self,
            parent_asin,
            listing,
            keywords,
            *,
            timeout_override=None,
        ):
            captured_batches.append(list(keywords))
            return {
                "keywords": [
                    {
                        "keyword_text": kw,
                        "semantic_conflict": "pass",
                        "conflict_reason": "",
                        "semantic_core": True,
                        "semantic_evidence": ["语义核心"],
                    }
                    for kw in keywords
                ],
                "error": "",
            }

    monkeypatch.setattr(CK, "CoreKeywordFetcher", FakeFetcher)
    monkeypatch.setattr(CK, "reasoner", FakeReasoner())

    try:
        out = await CK.run_core_keyword_analysis(
            "B0TEST",
            "SKU-1",
            1622,
            target_acos=40.0,
        )
    finally:
        settings.core_keyword_analyze_enabled = old_analyze
        settings.core_keyword_enabled = old_consume

    assert out["ok"] is True
    assert [len(batch) for batch in captured_batches] == [20, 15]
    assert [kw for batch in captured_batches for kw in batch] == [f"keyword {i}" for i in range(35)]
    assert out["total_keyword_count"] == 35
    assert out["core_keyword_count"] == 30
    assert sum(1 for row in out["labels"] if row["is_core"]) == 30


@pytest.mark.asyncio
async def test_offline_analysis_passes_locked_and_vetoed_terms_to_fetcher(monkeypatch):
    old_analyze = settings.core_keyword_analyze_enabled
    settings.core_keyword_analyze_enabled = True
    captured: dict[str, set[str]] = {}

    class FakeFetcher:
        async def fetch(self, parent_asin, parent_seller_sku, shop_id, *, excluded_keyword_norms=None):
            captured["excluded"] = excluded_keyword_norms or set()
            return FetchResult(
                context=FetchedContext(site_code="Amazon_US"),
                listing=ListingProductInfo(), campaigns=[], flow_keywords=[],
            )

    monkeypatch.setattr(CK, "CoreKeywordFetcher", FakeFetcher)
    monkeypatch.setattr(
        ErpDualWriterRepository,
        "fetch_core_keyword_exclusion_set",
        lambda *args: {"locked kw", "vetoed kw"},
        raising=False,
    )
    try:
        await CK.run_core_keyword_analysis("B0TEST", "SKU-1", 1622)
    finally:
        settings.core_keyword_analyze_enabled = old_analyze

    assert captured["excluded"] == {"locked kw", "vetoed kw"}


@pytest.mark.asyncio
async def test_management_api_returns_repository_management_payload(monkeypatch):
    payload = {
        "effective_core_count": 1,
        "limit": 30,
        "latest_task": {"id": "ckt-new", "finished_at": "2026-07-16T00:00:00"},
        "rows": [],
        "word_pool": ["current core", "current non-core"],
    }

    class FakeRepository:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def list_core_keyword_management(self, parent_asin, parent_seller_sku, shop_id):
            assert (parent_asin, parent_seller_sku, shop_id) == ("B0TEST", "SKU-1", 1622)
            return payload

    monkeypatch.setattr("app.persistence.erp_writer.repository.ErpDualWriterRepository", FakeRepository)

    out = await core_keyword_api.get_core_keyword_management("B0TEST", "SKU-1", 1622)

    assert out == {"ok": True, **payload}


@pytest.mark.asyncio
async def test_policy_api_returns_http_409_for_a_stale_task(monkeypatch):
    class FakeRepository:
        def __init__(self, **kwargs): pass
        def upsert_core_keyword_policy(self, *args, **kwargs): raise RuntimeError("STALE_TASK")

    monkeypatch.setattr("app.persistence.erp_writer.repository.ErpDualWriterRepository", FakeRepository)
    out = await core_keyword_api.set_core_keyword_policy({
        "parent_asin": "B0TEST", "parent_seller_sku": "SKU-1", "shop_id": 1622,
        "keyword_text": "core kw", "state": "LOCKED", "expected_task_id": "ckt-old",
    })

    assert isinstance(out, JSONResponse)
    assert out.status_code == 409


@pytest.mark.asyncio
async def test_offline_analysis_refuses_when_analyze_switch_off(monkeypatch):
    old_analyze = settings.core_keyword_analyze_enabled
    settings.core_keyword_analyze_enabled = False

    class FailFetcher:
        async def fetch(self, parent_asin, parent_seller_sku, shop_id, *, excluded_keyword_norms=None):
            raise AssertionError("fetch should not run when analyze switch is off")

    monkeypatch.setattr(CK, "CoreKeywordFetcher", FailFetcher)
    try:
        out = await CK.run_core_keyword_analysis("B0TEST", "SKU-1", 1622)
    finally:
        settings.core_keyword_analyze_enabled = old_analyze

    assert out == {"ok": False, "error": "core_keyword_analyze_enabled is False"}


@pytest.mark.asyncio
async def test_core_keyword_api_validates_required_identity():
    out = await core_keyword_api.analyze_core_keywords({"parent_asin": "B0TEST"})

    assert out["ok"] is False
    assert "缺少必填入参" in out["error"]


@pytest.mark.asyncio
async def test_core_keyword_api_writes_successful_analysis(monkeypatch):
    saved: list[dict] = []

    async def fake_run_core_keyword_analysis(**kwargs):
        return {
            "ok": True,
            "task_id": "ckt123",
            "total_keyword_count": 2,
            "core_keyword_count": 1,
            "labels": [],
        }

    class FakeRepository:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def write_core_keyword_task(self, analysis):
            saved.append(analysis)

    monkeypatch.setattr(core_keyword_api, "run_core_keyword_analysis", fake_run_core_keyword_analysis)
    monkeypatch.setattr("app.persistence.erp_writer.repository.ErpDualWriterRepository", FakeRepository)

    out = await core_keyword_api.analyze_core_keywords({
        "parent_asin": "B0TEST",
        "parent_seller_sku": "SKU-1",
        "shop_id": 1622,
    })

    assert out["ok"] is True
    assert out["task_id"] == "ckt123"
    assert saved and saved[0]["task_id"] == "ckt123"


def test_fetch_core_keyword_set_returns_empty_when_consume_switch_off(monkeypatch):
    old_enabled = settings.core_keyword_enabled
    settings.core_keyword_enabled = False

    try:
        result = ErpDualWriterRepository.fetch_core_keyword_set("B0TEST", "SKU-1", 1622)
    finally:
        settings.core_keyword_enabled = old_enabled

    assert result == set()


def test_campaign_prompt_marks_only_offline_core_keywords():
    core_unit = CampaignUnit(
        campaign_name="camp-core",
        campaign_key="camp-core x B0C1",
        child_asin="B0C1",
        keyword_text="core kw",
        match_type="EXACT",
        current_bid=0.35,
        current_budget=5,
        perf_7d=CampaignPerf(cost=3, orders=1, acos=30.0),
    )
    normal_unit = core_unit.model_copy(update={
        "campaign_name": "camp-normal",
        "campaign_key": "camp-normal x B0C2",
        "child_asin": "B0C2",
        "keyword_text": "normal kw",
    })

    core_summary = LLMReasoner._campaign_to_prompt_dict(core_unit, is_core=True)
    normal_summary = LLMReasoner._campaign_to_prompt_dict(normal_unit, is_core=False)

    assert core_summary["is_core"] is True
    assert "is_core" not in normal_summary


def test_backfill_campaign_context_uses_core_keyword_set():
    item_core = CampaignAdjustmentItem(
        campaign_name="camp-core",
        campaign_key="camp-core x B0C1",
        keyword_text="core kw",
    )
    item_normal = CampaignAdjustmentItem(
        campaign_name="camp-normal",
        campaign_key="camp-normal x B0C2",
        keyword_text="normal kw",
    )
    unit_core = CampaignUnit(
        campaign_name="camp-core",
        campaign_key=item_core.campaign_key,
        child_asin="B0C1",
        keyword_text="core kw",
        match_type="EXACT",
    )
    unit_normal = CampaignUnit(
        campaign_name="camp-normal",
        campaign_key=item_normal.campaign_key,
        child_asin="B0C2",
        keyword_text="normal kw",
        match_type="EXACT",
    )

    campaign_step._backfill_campaign_adjustment_context(
        [item_core, item_normal],
        {
            unit_core.campaign_key: unit_core,
            unit_normal.campaign_key: unit_normal,
        },
        keyword_class_map={},
        rank_evidence_line=lambda cu: "",
        core_keyword_set={"core kw"},
    )

    assert item_core.is_core is True
    assert item_normal.is_core is False


def test_fetch_core_keyword_set_reads_latest_done_for_same_product_identity(monkeypatch):
    old_enabled = settings.core_keyword_enabled
    settings.core_keyword_enabled = True

    class FakeCursor:
        def __init__(self):
            self.sql = ""
            self.sqls = []
            self.params = ()

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, sql, params):
            self.sql = sql
            self.sqls.append(sql)
            self.params = params

        def fetchall(self):
            return [{"keyword_text": "core kw"}, {"keyword_text": "other core"}]

    class FakeConn:
        def __init__(self):
            self.cursor_obj = FakeCursor()
            self.closed = False

        def cursor(self):
            return self.cursor_obj

        def close(self):
            self.closed = True

    class FakeRepo:
        def __init__(self):
            self.conn = FakeConn()

        def _connect(self):
            return self.conn

    fake_repo = FakeRepo()
    monkeypatch.setattr("app.persistence.erp_writer.repository._get_repository", lambda: fake_repo)

    try:
        result = ErpDualWriterRepository.fetch_core_keyword_set("B0TEST", "SKU-1", 1622)
    finally:
        settings.core_keyword_enabled = old_enabled

    assert result == {"core kw", "other core"}
    assert any("t.status = 'DONE'" in sql for sql in fake_repo.conn.cursor_obj.sqls)
    assert any("ORDER BY started_at DESC, finished_at DESC, id DESC" in sql for sql in fake_repo.conn.cursor_obj.sqls)
    assert fake_repo.conn.cursor_obj.sqls[0]
    assert fake_repo.conn.cursor_obj.params == (
        "B0TEST", "SKU-1", 1622,
    )
    assert fake_repo.conn.closed is True


def test_write_core_keyword_task_replaces_retry_labels_and_updates_total_count():
    executed: list[tuple[str, tuple]] = []

    class FakeCursor:
        def __enter__(self): return self
        def __exit__(self, exc_type, exc, tb): return False
        def execute(self, sql, params=()): executed.append((sql, params))

    class FakeConn:
        def cursor(self): return FakeCursor()
        def commit(self): pass
        def close(self): pass

    repo = object.__new__(ErpDualWriterRepository)
    repo._connect = lambda: FakeConn()
    repo._sync_core_keyword_policies_after_task = lambda cur, analysis, now: None
    repo.write_core_keyword_task({
        "task_id": "ckt-same-day", "parent_asin": "B0TEST", "parent_seller_sku": "SKU-1",
        "shop_id": 1622, "site_code": "Amazon_US", "core_keyword_count": 1,
        "labels": [{
            "keyword_text": "new core", "semantic_conflict": "pass", "semantic_core": True,
            "data_core": False, "is_core": True,
        }],
    })

    sqls = [sql for sql, _ in executed]
    assert any("DELETE FROM t_advert_agent_core_keyword_label" in sql for sql in sqls)
    task_upsert = next(sql for sql in sqls if "INSERT INTO t_advert_agent_core_keyword_task" in sql)
    assert "total_keyword_count=VALUES(total_keyword_count)" in task_upsert

"""多源配额分桶选取 _select_by_quota 单测（纯函数，无 I/O）。

锁定计划中的必守隐患：
  H2  — 竞品词 natural_rank=None + 低搜索量，在配额分桶下不被全局排序挤掉。
  H7② — 竞品源关闭（competitor 桶空）时，其配额回补给其余源，总候选不缩水。
（H3 search_volume_map 副产出未断属 analyze_new_campaigns 集成行为，另由集成测试覆盖。）
"""

from app.data.campaign_fetcher import _reverse_keyword_rows
from app.models.campaign import NewCampaignCandidate
from app.workflow.steps.campaign_new import _merge_candidate, _select_by_quota

QUOTAS = {"competitor": 20, "ranking_opportunity": 15, "flow": 5}
PRIORITY = ["competitor", "ranking_opportunity", "flow"]


def _cand(kw, source, rank=None, sv=0):
    return NewCampaignCandidate(
        keyword_text=kw, search_volume=sv, natural_rank=rank, source=source,
    )


def test_h2_competitor_low_signal_survives_quota():
    """H2：30 个高搜索量 flow 词 + 1 个 natural_rank=None/sv=0 的竞品词。
    纯全局排序(natural_rank is None, -sv)会把竞品词排到最后；配额分桶必须保住它。"""
    flow = [_cand(f"flow{i}", "flow", rank=None, sv=1000 - i) for i in range(30)]
    comp = [_cand("comp_kw", "competitor", rank=None, sv=0)]
    selected = _select_by_quota(flow + comp, 40, QUOTAS, PRIORITY)
    kws = {c.keyword_text for c in selected}
    assert "comp_kw" in kws, "竞品低信号词被全局排序挤掉了（H2 回归）"


def test_h7_competitor_disabled_quota_redistributes_no_shrink():
    """H7②：无竞品候选（源关）时，竞品 20 配额回补给 ranking/flow，总数填到 max_n 而非缩到 20。"""
    ranking = [_cand(f"r{i}", "ranking_opportunity", rank=30 + i) for i in range(20)]
    flow = [_cand(f"f{i}", "flow", sv=500 - i) for i in range(40)]
    selected = _select_by_quota(ranking + flow, 40, QUOTAS, PRIORITY)
    assert len(selected) == 40, f"竞品关闭后名额未回补，总候选缩水到 {len(selected)}"


def test_quota_respects_caps_and_priority():
    """三源都充足时：竞品先占 20、ranking 占 15、flow 占 5，总 40。"""
    comp = [_cand(f"c{i}", "competitor", sv=100) for i in range(30)]
    ranking = [_cand(f"r{i}", "ranking_opportunity", rank=30) for i in range(30)]
    flow = [_cand(f"f{i}", "flow", sv=100) for i in range(30)]
    selected = _select_by_quota(comp + ranking + flow, 40, QUOTAS, PRIORITY)
    by_src = {"competitor": 0, "ranking_opportunity": 0, "flow": 0}
    for c in selected:
        by_src[c.source] += 1
    assert len(selected) == 40
    assert by_src["competitor"] == 20
    assert by_src["ranking_opportunity"] == 15
    assert by_src["flow"] == 5


def test_ranking_refilled_when_competitor_short():
    """竞品仅 5 个（欠额 15）→ 剩余名额优先回补 ranking（事实相关），再 flow。"""
    comp = [_cand(f"c{i}", "competitor", sv=100) for i in range(5)]
    ranking = [_cand(f"r{i}", "ranking_opportunity", rank=30) for i in range(40)]
    flow = [_cand(f"f{i}", "flow", sv=100) for i in range(40)]
    selected = _select_by_quota(comp + ranking + flow, 40, QUOTAS, PRIORITY)
    by_src = {"competitor": 0, "ranking_opportunity": 0, "flow": 0}
    for c in selected:
        by_src[c.source] += 1
    assert len(selected) == 40
    assert by_src["competitor"] == 5
    # 竞品欠额 15 → ranking 先回补（>基础配额 15），flow 仍至少基础 5
    assert by_src["ranking_opportunity"] > 15
    assert by_src["flow"] >= 5


# ── 竞品反查解析（live 核实的真实层级 data.data.list + 字段 searches/bid）──

def test_reverse_keyword_rows_real_nesting():
    """真实层级 value.data.data.list；字段 keyword/searches/bid。"""
    payload = {"success": True, "found": True, "data": {"code": 200, "data": {
        "total_keywords": 2, "list": [
            {"keyword": "strapless bra", "searches": 1729210, "bid": 1.4},
            {"keyword": "sticky bra", "searches": 981477, "bid": 0.71},
        ]}}}
    rows = _reverse_keyword_rows(payload)
    assert [r["keyword"] for r in rows] == ["strapless bra", "sticky bra"]
    assert rows[0]["searches"] == 1729210 and rows[0]["bid"] == 1.4


def test_reverse_keyword_rows_empty_and_envelope():
    """niche ASIN list 空 → []；纯 envelope 无 list → []（不把 envelope 当成一行）。"""
    assert _reverse_keyword_rows({"data": {"data": {"list": []}}}) == []
    assert _reverse_keyword_rows({"success": True, "found": False}) == []
    assert _reverse_keyword_rows(None) == []


# ── 去重合并来源（Q1）──

def test_merge_keeps_one_merges_source_highest_priority():
    """同词 flow→competitor：留一条，bucket 升为 competitor，source_reason 合并，bid 补入。"""
    by_kw: dict = {}
    _merge_candidate(by_kw, NewCampaignCandidate(
        keyword_text="kw", search_volume=100, source="flow", source_reason="流量词库(搜索量100)"))
    _merge_candidate(by_kw, NewCampaignCandidate(
        keyword_text="kw", search_volume=0, suggested_bid=1.4, source="competitor",
        source_reason="竞品B0X反查", trigger_scene="COMPETITOR_INTERCEPT_WINDOW"))
    assert len(by_kw) == 1
    c = by_kw["kw"]
    assert c.source == "competitor"                         # 归最高优先级源
    assert "流量词库" in c.source_reason and "竞品B0X反查" in c.source_reason  # 来源合并
    assert c.suggested_bid == 1.4                           # 补 bid
    assert c.search_volume == 100                           # 取较大
    assert c.trigger_scene == "COMPETITOR_INTERCEPT_WINDOW"

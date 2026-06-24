"""目标 ACOS 区间化回归测试（交接文档 §20，2026-06-18 修复4 / 2026-06-24 上限口径修正）。

锁住 `compute_target_acos_band` 的区间语义 + `TargetAcosRecommender.recommend`
Step7 钳制契约。该逻辑已上线 prod（2026-06-22）但此前零测试覆盖。

设计意图（勿改）：
- 上限 = 阶段上限（KB03 §2，具体值，优先生效）；阶段未定义/未归一化时回落层级**默认**上限
  （KB03 §1：长尾 P3=30、其余 40）。2026-06-24 运营确认：阶段上限优先，层级不再 min 封顶
  非长尾到 40——清货期60/测试期50 须生效，长尾 P3 清货期亦随阶段到 60%。
- 下限 = max(global_min, 各广告目的下限的最大值)，且不超过上限（上限优先）。
- recommend 最终值必落在 [下限, 上限] 且为 increment(5%) 的倍数。
"""

import pytest

from app.core.recommender import TargetAcosRecommender, compute_target_acos_band
from app.models.asin_data import ASINData, AdData


# 合成 cfg —— 复刻 thresholds.toml [target_acos] 结构，隔离纯函数边界逻辑
CFG = {
    "min_acos": 25,
    "max_acos": 100,
    "increment": 5,
    "stage_ceilings": {
        "测试期": 50, "推进期": 40, "收割利润期": 40, "维持期": 40, "清货期": 60,
    },
    "purpose_floors": {"盈利型": 25, "转化型": 25, "排名型": 35, "引流型": 40},
}


# ── A. 纯函数边界逻辑（合成 cfg）────────────────────────────────

def test_band_stage_ceiling_takes_effect():
    # 阶段上限优先：清货期 60 对非长尾产品直接生效（2026-06-24 修正核心）
    assert compute_target_acos_band(CFG, "清货期", "常规产品 (P2)", ["盈利型"]) == (25, 60)


def test_band_test_stage_50_takes_effect():
    # 测试期 50 生效（旧实现会被层级 40 封顶）
    assert compute_target_acos_band(CFG, "测试期", "常规产品 (P2)", ["盈利型"]) == (25, 50)


def test_band_longtail_follows_stage_when_defined():
    # 阶段已定义时，长尾层级 30 不再压制 → 跟随阶段（2026-06-24 决策）
    assert compute_target_acos_band(CFG, "清货期", "长尾产品 (P3)", ["盈利型"]) == (25, 60)
    assert compute_target_acos_band(CFG, "推进期", "长尾产品 (P3)", ["盈利型"]) == (25, 40)


def test_band_level_default_only_when_stage_missing():
    # 阶段缺失/未归一化 → 回落层级默认：P3=30，其余=40；子串匹配防枚举键漂移
    assert compute_target_acos_band(CFG, "未归一化旧值", "长尾产品 (P3)", ["盈利型"])[1] == 30
    assert compute_target_acos_band(CFG, "未归一化旧值", "P3", ["盈利型"])[1] == 30
    assert compute_target_acos_band(CFG, "未归一化旧值", "常规产品 (P2)", ["盈利型"])[1] == 40


def test_band_multi_purpose_takes_max_floor():
    # 多目的取下限最大值：盈利25 + 排名35 → floor=35
    assert compute_target_acos_band(CFG, "测试期", "常规产品 (P2)", ["盈利型", "排名型"]) == (35, 50)


def test_band_floor_never_exceeds_ceiling():
    # 上限优先：引流下限40 但阶段缺失+长尾兜底30 → floor 被钳到 30 → (30, 30)
    assert compute_target_acos_band(CFG, "未归一化旧值", "长尾产品 (P3)", ["引流型"]) == (30, 30)


def test_band_unknown_stage_none_level_defaults_to_40():
    assert compute_target_acos_band(CFG, "不存在的阶段", None, None) == (25, 40)


def test_band_no_purpose_uses_global_min():
    assert compute_target_acos_band(CFG, "推进期", None, []) == (25, 40)


def test_band_global_max_truncates_ceiling():
    cfg = dict(CFG, max_acos=35)
    # 测试期50 但 global_max 35 → ceiling=35
    _, ceiling = compute_target_acos_band(cfg, "测试期", None, ["盈利型"])
    assert ceiling == 35


# ── B. 真实 toml 同步（防 thresholds.toml 与代码漂移）──────────

@pytest.fixture
def real_cfg():
    return TargetAcosRecommender().cfg


# 阶段上限优先：清货期60/测试期50 生效；长尾 P3 清货期亦随阶段（2026-06-24 决策）。
@pytest.mark.parametrize("stage,level,purposes,expected", [
    ("测试期", "常规产品 (P2)", ["盈利型"], (25, 50)),
    ("推进期", None, ["引流型"], (40, 40)),
    ("清货期", None, ["盈利型"], (25, 60)),
    ("清货期", "长尾产品 (P3)", ["盈利型"], (25, 60)),
    ("推进期", "长尾产品 (P3)", ["盈利型"], (25, 40)),
])
def test_real_toml_band_matches_kb_anchors(real_cfg, stage, level, purposes, expected):
    assert compute_target_acos_band(real_cfg, stage, level, purposes) == expected


# ── C. recommend() Step7 钳制契约（端到端）────────────────────

def _asin(stage="推进期", level="常规产品 (P2)", acos=30.0):
    return ASINData(
        asin="B0BANDTEST",
        keywords=[],
        keyword_count=5,
        product_stage=stage,
        season_stage="淡季",
        product_level=level,
        ad_data=AdData(acos=acos, spend=200, orders=10),
    )


@pytest.mark.parametrize("stage,level,purposes,acos", [
    ("推进期", "常规产品 (P2)", ["盈利型"], 30.0),
    ("测试期", "常规产品 (P2)", ["引流型"], 55.0),
    ("清货期", "长尾产品 (P3)", ["盈利型"], 12.0),
])
def test_recommend_result_within_band_and_5pct(stage, level, purposes, acos):
    rec = TargetAcosRecommender()
    floor, ceiling = compute_target_acos_band(rec.cfg, stage, level, purposes)
    result = rec.recommend(_asin(stage, level, acos), purposes)
    assert floor <= result.recommended_target <= ceiling
    assert result.recommended_target % 5 == 0


def test_recommend_extreme_current_acos_stays_in_band():
    # 当前 ACOS 远高于上限，最终推荐仍不得越出区间上限
    rec = TargetAcosRecommender()
    stage, level, purposes = "推进期", "长尾产品 (P3)", ["盈利型"]
    floor, ceiling = compute_target_acos_band(rec.cfg, stage, level, purposes)
    result = rec.recommend(_asin(stage, level, acos=95.0), purposes)
    assert result.recommended_target <= ceiling
    assert result.recommended_target >= floor

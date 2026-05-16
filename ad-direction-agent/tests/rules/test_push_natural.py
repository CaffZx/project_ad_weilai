import pytest
from app.rules.push_natural import (
    check_rising_keywords,
    check_budget_ratio,
    check_ad_driven_keywords,
    check_rising_keywords_acos,
    check_rank_headroom,
)
from app.config.settings import settings


@pytest.mark.asyncio
async def test_pn_1_has_rising_keywords(standard_asin):
    thresholds = settings.thresholds_config
    result = await check_rising_keywords(standard_asin, thresholds)
    assert result is not None
    assert result.level == "confirmed"
    assert "上升" in result.message or "自然位" in result.message


@pytest.mark.asyncio
async def test_pn_1_no_rising_keywords(stable_asin):
    thresholds = settings.thresholds_config
    result = await check_rising_keywords(stable_asin, thresholds)
    assert result is not None
    assert result.level in ("suggest_optimize", "confirmed")


@pytest.mark.asyncio
async def test_pn_2_budget_reasonable(standard_asin):
    thresholds = settings.thresholds_config
    result = await check_budget_ratio(standard_asin, thresholds, {"budget_ratio": 20})
    assert result is not None
    assert result.level == "confirmed"


@pytest.mark.asyncio
async def test_pn_2_budget_too_high(standard_asin):
    thresholds = settings.thresholds_config
    result = await check_budget_ratio(standard_asin, thresholds, {"budget_ratio": 80})
    assert result is not None
    assert result.level == "suggest_optimize"


@pytest.mark.asyncio
async def test_pn_3_ad_driven_keywords(standard_asin):
    thresholds = settings.thresholds_config
    result = await check_ad_driven_keywords(standard_asin, thresholds)
    assert result is not None


@pytest.mark.asyncio
async def test_pn_5_rank_headroom(standard_asin):
    thresholds = settings.thresholds_config
    result = await check_rank_headroom(standard_asin, thresholds)
    assert result is not None
    assert result.level == "confirmed"

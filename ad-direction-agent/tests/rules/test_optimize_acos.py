import pytest
from app.rules.optimize_acos import (
    check_acos_overage,
    check_high_spend_zero_conversion,
    check_bid_headroom,
    check_negative_keyword_opportunities,
)
from app.config.settings import settings


@pytest.mark.asyncio
async def test_oa_1_force_correct_on_high_acos(high_acos_asin):
    thresholds = settings.thresholds_config
    result = await check_acos_overage(high_acos_asin, thresholds)
    assert result is not None
    assert result.level == "force_correct"


@pytest.mark.asyncio
async def test_oa_1_confirmed_on_standard(standard_asin):
    thresholds = settings.thresholds_config
    result = await check_acos_overage(standard_asin, thresholds)
    assert result is not None
    # Standard ASIN has some keywords with ACOS > 30% (e.g., maxi dress 35%)
    assert result.level in ("confirmed", "suggest_optimize")


@pytest.mark.asyncio
async def test_oa_2_wasteful_keywords(high_acos_asin):
    thresholds = settings.thresholds_config
    result = await check_high_spend_zero_conversion(high_acos_asin, thresholds)
    assert result is not None
    assert result.level == "force_correct"


@pytest.mark.asyncio
async def test_oa_3_bid_headroom(high_acos_asin):
    thresholds = settings.thresholds_config
    result = await check_bid_headroom(high_acos_asin, thresholds)
    assert result is not None


@pytest.mark.asyncio
async def test_oa_4_negative_opportunities(high_acos_asin):
    thresholds = settings.thresholds_config
    result = await check_negative_keyword_opportunities(high_acos_asin, thresholds)
    assert result is not None

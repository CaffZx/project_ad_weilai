import pytest
from app.rules.balance_maintain import (
    check_fluctuation,
    check_abnormal_signals,
    check_parameters_reasonable,
    check_rank_stability,
    check_acos_tolerance,
)
from app.config.settings import settings


@pytest.mark.asyncio
async def test_bm_1_stable_fluctuation(stable_asin):
    thresholds = settings.thresholds_config
    result = await check_fluctuation(stable_asin, thresholds)
    assert result is not None


@pytest.mark.asyncio
async def test_bm_2_no_abnormal(stable_asin):
    thresholds = settings.thresholds_config
    result = await check_abnormal_signals(stable_asin, thresholds)
    assert result is not None
    assert result.level == "confirmed"


@pytest.mark.asyncio
async def test_bm_3_reasonable_acos(stable_asin):
    thresholds = settings.thresholds_config
    result = await check_parameters_reasonable(stable_asin, thresholds)
    assert result is not None


@pytest.mark.asyncio
async def test_bm_4_stable_rank(stable_asin):
    thresholds = settings.thresholds_config
    result = await check_rank_stability(stable_asin, thresholds)
    assert result is not None


@pytest.mark.asyncio
async def test_bm_5_reasonable_tolerance(stable_asin):
    thresholds = settings.thresholds_config
    result = await check_acos_tolerance(stable_asin, thresholds, {"acos_tolerance": 5})
    assert result is not None
    assert result.level == "confirmed"


@pytest.mark.asyncio
async def test_bm_5_tolerance_too_low(stable_asin):
    thresholds = settings.thresholds_config
    result = await check_acos_tolerance(stable_asin, thresholds, {"acos_tolerance": 1})
    assert result is not None
    assert result.level == "suggest_optimize"

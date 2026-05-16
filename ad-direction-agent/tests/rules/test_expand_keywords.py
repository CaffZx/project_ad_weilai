import pytest
from app.rules.expand_keywords import (
    check_qualified_new_keywords,
    check_keyword_coverage,
    check_existing_keywords_acos,
    check_target_keyword_count,
)
from app.config.settings import settings


@pytest.mark.asyncio
async def test_ke_1_has_new_keywords(standard_asin):
    thresholds = settings.thresholds_config
    result = await check_qualified_new_keywords(standard_asin, thresholds)
    assert result is not None
    assert result.level == "confirmed"


@pytest.mark.asyncio
async def test_ke_1_no_new_keywords(stable_asin):
    thresholds = settings.thresholds_config
    result = await check_qualified_new_keywords(stable_asin, thresholds)
    assert result is not None


@pytest.mark.asyncio
async def test_ke_2_low_coverage(standard_asin):
    thresholds = settings.thresholds_config
    result = await check_keyword_coverage(standard_asin, thresholds)
    assert result is not None
    assert result.level == "confirmed"


@pytest.mark.asyncio
async def test_ke_3_health_acos(standard_asin):
    thresholds = settings.thresholds_config
    result = await check_existing_keywords_acos(standard_asin, thresholds)
    assert result is not None


@pytest.mark.asyncio
async def test_ke_4_reasonable_count(standard_asin):
    thresholds = settings.thresholds_config
    result = await check_target_keyword_count(standard_asin, thresholds, {"target_count": 5})
    assert result is not None
    assert result.level == "confirmed"


@pytest.mark.asyncio
async def test_ke_4_too_many(standard_asin):
    thresholds = settings.thresholds_config
    result = await check_target_keyword_count(standard_asin, thresholds, {"target_count": 20})
    assert result is not None
    assert result.level == "force_correct"

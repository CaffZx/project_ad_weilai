import pytest
from app.models.asin_data import ASINData
from app.rules import cross_tag as cross_rules
from app.config.settings import settings


@pytest.fixture
def test_stage_asin(standard_asin):
    """测试期 ASIN"""
    standard_asin.product_stage = "测试期"
    standard_asin.ad_purpose = "盈利型"
    return standard_asin


@pytest.fixture
def harvest_asin(standard_asin):
    """达成期 ASIN"""
    standard_asin.product_stage = "达成期"
    return standard_asin


@pytest.mark.asyncio
async def test_cross_1_test_stage_force_correct(test_stage_asin):
    thresholds = settings.thresholds_config
    result = await cross_rules.test_stage_ad_purpose(test_stage_asin, thresholds)
    assert result is not None
    assert result.level == "force_correct"


@pytest.mark.asyncio
async def test_cross_1_test_stage_pass(standard_asin):
    """非测试期/起步期应跳过"""
    thresholds = settings.thresholds_config
    result = await cross_rules.test_stage_ad_purpose(standard_asin, thresholds)
    assert result is None


@pytest.mark.asyncio
async def test_cross_4_broad_in_test_force(test_stage_asin):
    thresholds = settings.thresholds_config
    test_stage_asin.target_keyword_strategy = "Broad"
    result = await cross_rules.broad_keyword_stage_check(test_stage_asin, thresholds)
    assert result is not None
    assert result.level == "force_correct"

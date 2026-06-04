import os

# 测试收集阶段避免模块级 DataAggregator 连接真实 DB
os.environ.setdefault("DATA_SOURCE", "mock")

import pytest
from app.data.mock import MockAdapter
from app.models.asin_data import ASINData


@pytest.fixture
def mock_adapter():
    return MockAdapter()


@pytest.fixture
def standard_asin(mock_adapter):
    """标准 ASIN — 适合推进自然位"""
    return mock_adapter._scenario_standard()


@pytest.fixture
def high_acos_asin(mock_adapter):
    """高 ACOS ASIN — 适合优化 ACOS"""
    return mock_adapter._scenario_high_acos()


@pytest.fixture
def stable_asin(mock_adapter):
    """稳定态 ASIN — 适合平衡维持"""
    return mock_adapter._scenario_stable()


@pytest.fixture
def missing_data_asin(mock_adapter):
    """数据缺失 ASIN — 测试降级场景"""
    return mock_adapter._scenario_missing()

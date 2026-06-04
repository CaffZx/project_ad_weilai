import pytest
from app.data.mock import MockAdapter


@pytest.mark.asyncio
async def test_mock_adapter_returns_data():
    adapter = MockAdapter()
    data = await adapter.fetch_asin_data("0XSTANDARD")
    assert data.asin == "0XSTANDARD"
    assert data.keyword_count is not None
    assert len(data.keywords) > 0
    assert data.ad_data is not None


@pytest.mark.asyncio
async def test_mock_adapter_high_acos():
    adapter = MockAdapter()
    data = await adapter.fetch_asin_data("0XHIGHACOS")
    assert data.ad_data.acos is not None
    assert data.ad_data.acos > 30


@pytest.mark.asyncio
async def test_mock_adapter_stable():
    adapter = MockAdapter()
    data = await adapter.fetch_asin_data("0XSTABLE")
    assert data.ad_data.acos is not None
    assert data.ad_data.acos < 20


@pytest.mark.asyncio
async def test_mock_adapter_missing():
    adapter = MockAdapter()
    data = await adapter.fetch_asin_data("0XMISSING")
    assert data.data_missing is True
    assert len(data.missing_fields) > 0


@pytest.mark.asyncio
async def test_mock_adapter_unknown_asin_fallsback():
    adapter = MockAdapter()
    data = await adapter.fetch_asin_data("UNKNOWN")
    # Should return standard scenario as fallback
    assert data.asin == "UNKNOWN"

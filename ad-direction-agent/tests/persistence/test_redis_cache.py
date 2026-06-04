import pytest
from unittest.mock import AsyncMock, patch

from app.models.asin_data import ASINData
from app.persistence.redis_cache import AsinDataCache


@pytest.mark.asyncio
async def test_lru_fallback_when_redis_disabled():
    cache = AsinDataCache()
    with patch("app.persistence.redis_cache.settings") as mock_settings:
        mock_settings.redis_enabled = False
        mock_settings.redis_lru_ttl = 60
        mock_settings.redis_short_ttl = 7200
        mock_settings.redis_partial_ttl = 900
        data = ASINData(asin="B001", partial_failures=[])
        await cache.set("B001", data, days=7)
        got = await cache.get("B001", days=7)
        assert got is not None
        assert got.asin == "B001"


@pytest.mark.asyncio
async def test_partial_ttl_key():
    cache = AsinDataCache()
    mock_redis = AsyncMock()
    mock_redis.ping = AsyncMock()
    mock_redis.setex = AsyncMock()
    mock_redis.get = AsyncMock(return_value=None)
    mock_redis.delete = AsyncMock()
    cache._redis = mock_redis
    cache._redis_ok = True
    data = ASINData(asin="B002", partial_failures=["mcp:flow_keywords:timeout"])
    with patch("app.persistence.redis_cache.settings") as mock_settings:
        mock_settings.redis_enabled = True
        mock_settings.redis_lru_ttl = 30
        mock_settings.redis_partial_ttl = 900
        mock_settings.redis_short_ttl = 7200
        await cache.set("B002", data, days=7)
    mock_redis.setex.assert_called()
    call_args = mock_redis.setex.call_args_list[0]
    assert "partial" in call_args[0][0]

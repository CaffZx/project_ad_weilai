import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.models.asin_data import ASINData


@pytest.mark.asyncio
async def test_fetch_phased_prefer_db():
    from app.data.phased_fetcher import fetch_phased

    mock_data = ASINData(asin="B0TEST", data_freshness="fresh")
    with patch("app.data.db_adapter.DbAdapter") as mock_db:
        mock_db.return_value.fetch_asin_data = AsyncMock(return_value=mock_data)
        result = await fetch_phased("B0TEST", days=7, prefer_db=True)
        assert result.asin == "B0TEST"
        mock_db.return_value.fetch_asin_data.assert_called_once()

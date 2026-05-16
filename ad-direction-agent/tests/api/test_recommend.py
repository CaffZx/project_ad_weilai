import httpx
import pytest


@pytest.mark.asyncio
async def test_recommend_returns_scores():
    async with httpx.AsyncClient(base_url="http://127.0.0.1:8001") as client:
        r = await client.post(
            "/api/v1/agent/ad-direction/recommend",
            json={"asin": "B0DNZNWGX5"},
        )
    assert r.status_code == 200
    data = r.json()
    assert "recommended_direction" in data
    assert len(data["directions"]) == 4
    for d in data["directions"]:
        assert "suitability_score" in d
        assert d["suitability"] in ("recommended", "available", "not_recommended")


@pytest.mark.asyncio
async def test_recommend_high_acos():
    async with httpx.AsyncClient(base_url="http://127.0.0.1:8001") as client:
        r = await client.post(
            "/api/v1/agent/ad-direction/recommend",
            json={"asin": "B0DNZNWGX5"},
        )
    assert r.status_code == 200
    data = r.json()
    acos_scores = [d for d in data["directions"] if d["id"] == "optimize_acos"]
    assert len(acos_scores) > 0
    # B0DNZNWGX5 has ACOS 35.4% → +30 base score
    assert acos_scores[0]["suitability_score"] > 20


@pytest.mark.asyncio
async def test_recommend_missing_data():
    async with httpx.AsyncClient(base_url="http://127.0.0.1:8001") as client:
        r = await client.post(
            "/api/v1/agent/ad-direction/recommend",
            json={"asin": "B0NONEXISTENT"},
        )
    assert r.status_code == 200

import httpx
import pytest


@pytest.mark.asyncio
async def test_confirm_returns_decision():
    async with httpx.AsyncClient(base_url="http://127.0.0.1:8001") as client:
        r = await client.post(
            "/api/v1/agent/ad-direction/confirm",
            json={
                "asin": "0XSTANDARD",
                "direction": "push_natural",
                "sub_options": {"top_keywords_count": 3, "budget_ratio": 20},
                "long_term_tags": {},
                "special_scenario": "无",
            },
        )
    assert r.status_code == 200
    data = r.json()
    assert "decision_package" in data
    assert "tasks" in data["decision_package"]
    assert len(data["decision_package"]["tasks"]) > 0


@pytest.mark.asyncio
async def test_confirm_high_acos():
    async with httpx.AsyncClient(base_url="http://127.0.0.1:8001") as client:
        r = await client.post(
            "/api/v1/agent/ad-direction/confirm",
            json={
                "asin": "0XHIGHACOS",
                "direction": "optimize_acos",
                "sub_options": {"methods": ["negative_keywords", "reduce_bid"], "acos_threshold": 40, "cvr_threshold": 3},
                "long_term_tags": {},
                "special_scenario": "无",
            },
        )
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_confirm_stable():
    async with httpx.AsyncClient(base_url="http://127.0.0.1:8001") as client:
        r = await client.post(
            "/api/v1/agent/ad-direction/confirm",
            json={
                "asin": "0XSTABLE",
                "direction": "balance_maintain",
                "sub_options": {"acos_tolerance": 5},
                "long_term_tags": {},
                "special_scenario": "无",
            },
        )
    assert r.status_code == 200

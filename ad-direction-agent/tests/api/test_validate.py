import httpx
import pytest


@pytest.mark.asyncio
async def test_validate_push_natural():
    async with httpx.AsyncClient(base_url="http://127.0.0.1:8001") as client:
        r = await client.post(
            "/api/v1/agent/ad-direction/validate",
            json={
                "asin": "0XSTANDARD",
                "direction": "push_natural",
                "sub_options": {"top_keywords_count": 3, "budget_ratio": 20},
                "long_term_tags": {"product_level": "腰部"},
                "special_scenario": "无",
            },
        )
    assert r.status_code == 200
    data = r.json()
    assert data["direction"] == "push_natural"
    assert len(data["items"]) > 0


@pytest.mark.asyncio
async def test_validate_high_acos_force():
    async with httpx.AsyncClient(base_url="http://127.0.0.1:8001") as client:
        r = await client.post(
            "/api/v1/agent/ad-direction/validate",
            json={
                "asin": "0XHIGHACOS",
                "direction": "optimize_acos",
                "sub_options": {"methods": ["negative_keywords"], "acos_threshold": 40},
                "long_term_tags": {},
                "special_scenario": "无",
            },
        )
    assert r.status_code == 200
    data = r.json()
    assert data["overall_level"] == "force_correct"


@pytest.mark.asyncio
async def test_validate_stable_confirmed():
    async with httpx.AsyncClient(base_url="http://127.0.0.1:8001") as client:
        r = await client.post(
            "/api/v1/agent/ad-direction/validate",
            json={
                "asin": "0XSTABLE",
                "direction": "balance_maintain",
                "sub_options": {"acos_tolerance": 5},
                "long_term_tags": {},
                "special_scenario": "无",
            },
        )
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_validate_missing_data():
    async with httpx.AsyncClient(base_url="http://127.0.0.1:8001") as client:
        r = await client.post(
            "/api/v1/agent/ad-direction/validate",
            json={
                "asin": "0XMISSING",
                "direction": "expand_keywords",
                "sub_options": {},
                "long_term_tags": {},
                "special_scenario": "无",
            },
        )
    assert r.status_code == 200

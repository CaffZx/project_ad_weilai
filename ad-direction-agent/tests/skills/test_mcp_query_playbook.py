"""Tests for mcp-query skill playbook loading and validation."""

from pathlib import Path

import pytest

from app.skills.loader import default_skills_dir, load_playbook
from app.skills.models import SkillPlaybook
from app.skills.validate import validate_playbook


@pytest.fixture
def skills_dir() -> Path:
    return default_skills_dir()


def test_load_mcp_query_playbook(skills_dir: Path):
    pb = load_playbook("mcp-query", skills_dir)
    assert pb.name == "mcp-query"
    assert pb.version == 1
    assert pb.phase("context") is not None
    assert "listing_basic_info_v2" in pb.bootstrap_tools
    assert pb.fallback.on_mcp_failure == "none"


def test_playbook_resolves_settings_timeouts(skills_dir: Path):
    pb = load_playbook("mcp-query", skills_dir)
    ctx = pb.phase("context")
    assert ctx is not None
    assert ctx.timeout_seconds >= 3.0
    bootstrap = pb.phase("mcp_bootstrap")
    assert bootstrap is not None
    assert bootstrap.timeout_seconds >= 10.0


def test_validate_playbook_no_errors(skills_dir: Path):
    pb = load_playbook("mcp-query", skills_dir)
    assert validate_playbook(pb) == []


@pytest.mark.asyncio
async def test_fetch_phased_delegates_to_skill(monkeypatch):
    from app.data.phased_fetcher import fetch_phased
    from app.models.asin_data import ASINData

    called = {}

    async def fake_run(skill_name, **kwargs):
        called["skill"] = skill_name
        called.update(kwargs)
        return ASINData(asin=kwargs.get("asin", ""), data_freshness="fresh")

    monkeypatch.setattr("app.config.settings.settings.skills_enabled", True)
    monkeypatch.setattr(
        "app.skills.registry.skill_registry.run",
        fake_run,
    )

    result = await fetch_phased("B0TEST", days=7)
    assert called["skill"] == "mcp-query"
    assert called["asin"] == "B0TEST"
    assert result.data_freshness == "fresh"

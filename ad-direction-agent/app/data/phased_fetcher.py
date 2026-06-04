"""分阶段抓数 — 委托 mcp-query Skill 执行器（playbook.yaml）。"""

from __future__ import annotations

from app.models.asin_data import ASINData


async def fetch_phased(
    asin: str,
    meta_filter: list[str] | None = None,
    days: int = 7,
    *,
    prefer_db: bool = False,
) -> ASINData:
    """Thin wrapper: run mcp-query skill playbook."""
    from app.config.settings import settings

    if settings.skills_enabled:
        from app.skills.registry import skill_registry

        return await skill_registry.run(
            "mcp-query",
            asin=asin,
            meta_filter=meta_filter,
            days=days,
            prefer_db=prefer_db,
        )

    # Fallback if skills disabled (legacy inline path removed — use DbAdapter only)
    from app.data.db_adapter import DbAdapter

    data = await DbAdapter().fetch_asin_data(asin, meta_filter=meta_filter, days=days)
    data.data_freshness = "fresh"
    data.partial_failures = []
    return data

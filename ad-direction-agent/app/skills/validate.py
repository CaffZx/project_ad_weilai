"""Validate skill playbooks at startup."""

from __future__ import annotations

import logging

from app.data.mcp_mapping import TOOL_ARG_BUILDERS
from app.skills.loader import default_skills_dir, load_playbook
from app.skills.models import SkillPlaybook

_BOOTSTRAP_TOOLS = ("listing_basic_info", "listing_inventory")
_MCP_TOOL_TO_META: dict[str, str] = {
    "ad_product_report": "META_AD_PRODUCT",
    "ad_placement_report": "META_AD_PLACEMENT",
    "ad_keyword_report": "META_KW_AD",
    "ad_search_term_report": "META_AD_SEARCH_TERM",
    "keyword_competitors": "META_KW_COMPETITOR_RANK",
    "keyword_child_asins": "META_KW_COMPETITOR_RANK",
    "flow_keywords": "META_FLOW_KEYWORD",
    "product_sales": "META_TREND",
    "direct_competitors": "META_COMPETITOR",
    "own_keyword_flow": "META_FLOW_KEYWORD",
}

logger = logging.getLogger(__name__)


def validate_playbook(playbook: SkillPlaybook) -> list[str]:
    """Return list of validation errors (empty if ok)."""
    errors: list[str] = []
    known_tools = set(TOOL_ARG_BUILDERS.keys())
    bootstrap = set(playbook.bootstrap_tools)

    for tool in bootstrap:
        if tool not in known_tools:
            errors.append(f"bootstrap tool '{tool}' not in TOOL_ARG_BUILDERS")
        if tool not in _BOOTSTRAP_TOOLS:
            logger.warning(
                "playbook bootstrap tool '%s' not in known bootstrap tools",
                tool,
            )

    reports = playbook.phase("mcp_reports")
    if reports and reports.tools_from_meta:
        for tool in _MCP_TOOL_TO_META:
            if tool not in known_tools and tool not in bootstrap:
                errors.append(f"MCP_TOOL_TO_META tool '{tool}' not in TOOL_ARG_BUILDERS")

    if not playbook.phase("context"):
        errors.append("missing phase: context")

    return errors


def validate_all_skills(skills_dir=None) -> None:
    """Load and validate registered skills; log warnings on failure."""
    root = skills_dir or default_skills_dir()
    for name in ("mcp-query",):
        try:
            pb = load_playbook(name, root)
            errs = validate_playbook(pb)
            if errs:
                for e in errs:
                    logger.error("Skill playbook validation [%s]: %s", name, e)
            else:
                logger.info("Skill playbook validated: %s (%s)", name, pb.path)
        except FileNotFoundError as e:
            logger.warning("Skill playbook missing [%s]: %s", name, e)

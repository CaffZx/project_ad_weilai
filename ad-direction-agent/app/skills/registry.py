"""Skill registry — load playbooks and dispatch executors."""

from __future__ import annotations

import logging
from typing import Any

from app.config.settings import settings
from app.models.asin_data import ASINData
from app.skills.executors.mcp_query import McpQuerySkillExecutor
from app.skills.loader import default_skills_dir, load_playbook

logger = logging.getLogger(__name__)


class SkillRegistry:
    def __init__(self):
        self._executors: dict[str, McpQuerySkillExecutor] = {}
        self._loaded = False

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        skills_dir = default_skills_dir()
        pb = load_playbook("mcp-query", skills_dir)
        self._executors["mcp-query"] = McpQuerySkillExecutor(pb)
        self._loaded = True
        logger.info("SkillRegistry loaded: %s", list(self._executors.keys()))

    def reload(self) -> None:
        self._loaded = False
        self._executors.clear()
        self._ensure_loaded()

    async def run(self, skill_name: str, **kwargs: Any) -> ASINData:
        if not settings.skills_enabled:
            raise RuntimeError("skills_enabled is false")
        self._ensure_loaded()
        executor = self._executors.get(skill_name)
        if not executor:
            raise KeyError(f"Unknown skill: {skill_name}")
        return await executor.run(**kwargs)


skill_registry = SkillRegistry()

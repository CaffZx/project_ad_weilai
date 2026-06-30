"""Load and resolve skill playbooks from YAML."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

import yaml

from app.config.settings import settings
from app.skills.models import PlaybookFallback, PlaybookPhase, SkillPlaybook

logger = logging.getLogger(__name__)

_SETTING_REF = re.compile(r"^\$\{([A-Z0-9_]+)\}$")

# Env-style name -> Settings attribute
_SETTING_ATTR: dict[str, str] = {
    "MCP_CONTEXT_TIMEOUT": "mcp_context_timeout",
    "MCP_BOOTSTRAP_TIMEOUT": "mcp_bootstrap_timeout",
    "MCP_TOOL_TIMEOUT": "mcp_tool_timeout",
    "MCP_MAX_CONCURRENCY": "mcp_max_concurrency",
}


def default_skills_dir() -> Path:
    if settings.skills_dir:
        return Path(settings.skills_dir)
    from app.config.settings import BASE_DIR

    return BASE_DIR / "app" / "config" / "skills"


def _resolve_value(raw: Any) -> Any:
    if isinstance(raw, str):
        m = _SETTING_REF.match(raw.strip())
        if m:
            attr = _SETTING_ATTR.get(m.group(1), m.group(1).lower())
            return getattr(settings, attr, raw)
    if isinstance(raw, list):
        return [_resolve_value(v) for v in raw]
    if isinstance(raw, dict):
        return {k: _resolve_value(v) for k, v in raw.items()}
    return raw


def _parse_phase(data: dict) -> PlaybookPhase:
    tools = data.get("tools") or []
    return PlaybookPhase(
        id=str(data["id"]),
        source=data.get("source"),
        resolver=data.get("resolver"),
        tools=tuple(str(t) for t in tools),
        tools_from_meta=bool(data.get("tools_from_meta")),
        timeout_seconds=float(_resolve_value(data.get("timeout_seconds", 30))),
        concurrency=int(_resolve_value(data.get("concurrency", 8))),
    )


def load_playbook(skill_name: str, skills_dir: Path | None = None) -> SkillPlaybook:
    """Load playbook.yaml for a skill directory."""
    root = skills_dir or default_skills_dir()
    path = root / skill_name / "playbook.yaml"
    if not path.is_file():
        raise FileNotFoundError(f"Skill playbook not found: {path}")

    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    raw = _resolve_value(raw)
    phases = tuple(_parse_phase(p) for p in raw.get("phases", []))
    fb_raw = raw.get("fallback") or {}
    fallback = PlaybookFallback(
        on_mcp_failure=str(fb_raw.get("on_mcp_failure", "none")),
        enabled=bool(fb_raw.get("enabled", True)),
        full_scene_meta=bool(fb_raw.get("full_scene_meta", True)),
        timeout_seconds=float(fb_raw.get("timeout_seconds", 300)),
    )
    return SkillPlaybook(
        name=str(raw.get("name", skill_name)),
        version=int(raw.get("version", 1)),
        path=path,
        phases=phases,
        fallback=fallback,
    )

"""Playbook models for runtime skills."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class PlaybookPhase:
    id: str
    source: str | None = None
    resolver: str | None = None
    tools: tuple[str, ...] = ()
    tools_from_meta: bool = False
    timeout_seconds: float = 30.0
    concurrency: int = 8


@dataclass(frozen=True)
class PlaybookFallback:
    on_mcp_failure: str = "none"
    enabled: bool = True
    full_scene_meta: bool = True
    timeout_seconds: float = 300.0


@dataclass(frozen=True)
class SkillPlaybook:
    name: str
    version: int
    path: Path
    phases: tuple[PlaybookPhase, ...]
    fallback: PlaybookFallback

    def phase(self, phase_id: str) -> PlaybookPhase | None:
        for p in self.phases:
            if p.id == phase_id:
                return p
        return None

    @property
    def bootstrap_tools(self) -> tuple[str, ...]:
        p = self.phase("mcp_bootstrap")
        return p.tools if p else ()

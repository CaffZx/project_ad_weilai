"""版本公告 API — 列出可用公告文件"""

from pathlib import Path

from fastapi import APIRouter

ANNOUNCE_DIR = Path(__file__).resolve().parent.parent.parent.parent / "docs" / "announcements"

router = APIRouter()


@router.get("/announcements")
async def list_announcements():
    """列出所有版本公告文件，按版本倒序"""
    items = []
    if ANNOUNCE_DIR.exists():
        for fp in sorted(ANNOUNCE_DIR.glob("*.md"), reverse=True):
            version = fp.stem  # v2.2, v2.1, ...
            items.append({
                "filename": fp.name,
                "version": version,
                "url": f"/announcements/{fp.name}",
            })
    return items

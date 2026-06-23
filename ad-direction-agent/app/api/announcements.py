"""版本公告 API — 列出可用公告文件"""

from pathlib import Path

from fastapi import APIRouter

ANNOUNCE_DIR = Path(__file__).resolve().parent.parent.parent.parent / "docs" / "announcements"

# 始终置顶展示的文件名（不带后缀），按列表顺序排在最前；其余文件按名称倒序排列
PINNED_FIRST = ["操作说明"]

router = APIRouter()


@router.get("/announcements")
async def list_announcements():
    """列出所有版本公告文件。

    排序规则：
      - PINNED_FIRST 中的文件按列表顺序置顶（如 "操作说明" 永远第一）
      - 其余文件按文件名倒序（v3.1 > v2.6 > v2.5 ...）
    """
    if not ANNOUNCE_DIR.exists():
        return []
    all_files = list(ANNOUNCE_DIR.glob("*.md"))
    pinned_set = set(PINNED_FIRST)
    pinned_files = []
    for name in PINNED_FIRST:
        for fp in all_files:
            if fp.stem == name:
                pinned_files.append(fp)
                break
    other_files = sorted(
        [fp for fp in all_files if fp.stem not in pinned_set],
        reverse=True,
    )
    ordered = pinned_files + other_files
    return [
        {
            "filename": fp.name,
            "version": fp.stem,
            "url": f"/announcements/{fp.name}",
        }
        for fp in ordered
    ]

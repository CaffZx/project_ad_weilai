"""Shared paths for scripts under scripts/erp_db/."""
from __future__ import annotations

import sys
from pathlib import Path

ERP_DB_DIR = Path(__file__).resolve().parent
SCRIPTS_DIR = ERP_DB_DIR.parent
AD_DIRECTION_AGENT = SCRIPTS_DIR.parent
REPO_ROOT = AD_DIRECTION_AGENT.parent


def bootstrap_sys_path() -> None:
    root = str(AD_DIRECTION_AGENT)
    if root not in sys.path:
        sys.path.insert(0, root)

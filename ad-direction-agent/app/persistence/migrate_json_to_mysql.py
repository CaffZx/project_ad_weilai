"""一次性将 config/{asin}/*.json 迁移到 MySQL，完成后目录重命名为 _migrated。"""

from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path

from app.config.settings import settings
from app.persistence.state_manager import BASE_CONFIG_DIR, StateManager

logger = logging.getLogger(__name__)


def migrate_json_to_mysql() -> int:
    """返回迁移的 ASIN 数量。"""
    if (settings.state_backend or "json").lower() != "mysql":
        return 0
    config_dir = BASE_CONFIG_DIR
    if not config_dir.exists():
        return 0
    migrated_marker = config_dir / "_migrated"
    if migrated_marker.exists():
        return 0

    from app.persistence.mysql_state_manager import MySQLStateManager

    json_sm = StateManager()
    mysql_sm = MySQLStateManager()
    try:
        mysql_sm.ensure_schema()
    except Exception as e:  # noqa: BLE001
        logger.error("MySQL schema 初始化失败，跳过迁移: %s", e)
        return 0

    count = 0
    for asin_dir in sorted(config_dir.iterdir()):
        if not asin_dir.is_dir() or asin_dir.name.startswith("_"):
            continue
        asin = asin_dir.name
        lt = json_sm.get_long_term_config(asin)
        if lt:
            mysql_sm.set_long_term_config(asin, lt)
        wf = json_sm.get_workflow_state(asin)
        if wf:
            mysql_sm.set_workflow_state(asin, wf)
        p3_fp = asin_dir / "p3_recommendation.json"
        if p3_fp.exists():
            try:
                p3 = json.loads(p3_fp.read_text(encoding="utf-8"))
                mysql_sm.set_p3_recommendation(asin, p3)
            except Exception:
                pass
        acos_fp = asin_dir / "target_acos_override.json"
        if acos_fp.exists():
            try:
                acos = json.loads(acos_fp.read_text(encoding="utf-8"))
                if acos.get("value") is not None:
                    mysql_sm.set_target_acos_override(asin, int(acos["value"]))
            except Exception:
                pass
        count += 1
        dest = migrated_marker / asin
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            shutil.rmtree(dest)
        shutil.move(str(asin_dir), str(dest))
        logger.info("已迁移 ASIN 配置: %s", asin)

    migrated_marker.mkdir(parents=True, exist_ok=True)
    (migrated_marker / ".done").write_text("ok", encoding="utf-8")
    return count

"""StateManager 工厂 — 按 STATE_BACKEND 选择 JSON 或 MySQL 实现。"""

from __future__ import annotations

import logging

from app.config.settings import settings

logger = logging.getLogger(__name__)

_state_manager = None


def get_state_manager():
    global _state_manager
    if _state_manager is not None:
        return _state_manager
    backend = (settings.state_backend or "json").lower()
    if backend == "mysql":
        from app.persistence.mysql_state_manager import MySQLStateManager

        _state_manager = MySQLStateManager()
        logger.info("StateManager: MySQL backend")
        return _state_manager
    from app.persistence.state_manager import StateManager

    _state_manager = StateManager()
    logger.info("StateManager: JSON backend")
    return _state_manager


def reset_state_manager_singleton() -> None:
    """测试用：重置单例。"""
    global _state_manager
    _state_manager = None

"""JSON 文件持久化管理器

职责：
1. 长期配置（战略+策略层选择）→ config/{asin}/long_term_config.json
2. 工作流状态（当前步骤、已完成层、执行层选择）→ config/{asin}/workflow_state.json
"""

import json
import logging
import threading
from datetime import datetime, timezone, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

BASE_CONFIG_DIR = Path(__file__).resolve().parent.parent.parent / "config"


class StateManager:
    """管理 ASIN 级别的持久化状态"""

    def __init__(self, base_dir: Path | str | None = None):
        if base_dir is None:
            self.base_dir = BASE_CONFIG_DIR
        elif isinstance(base_dir, str):
            self.base_dir = Path(base_dir)
        else:
            self.base_dir = base_dir
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self._lock_factory = threading.Lock()
        self._locks: dict[str, threading.RLock] = {}

    def _get_lock(self, asin: str) -> threading.RLock:
        """获取 ASIN 级别的文件锁（线程安全创建，RLock 支持重入）"""
        with self._lock_factory:
            if asin not in self._locks:
                self._locks[asin] = threading.RLock()
        return self._locks[asin]

    def _asin_dir(self, asin: str) -> Path:
        return self.base_dir / asin

    def _ensure_asin_dir(self, asin: str) -> Path:
        d = self._asin_dir(asin)
        d.mkdir(parents=True, exist_ok=True)
        return d

    # ── 长期配置 ─────────────────────────────────────────

    def get_long_term_config(self, asin: str) -> dict:
        """读取战略+策略长期配置，不存在返回空dict"""
        with self._get_lock(asin):
            fp = self._asin_dir(asin) / "long_term_config.json"
            if not fp.exists():
                return {}
            try:
                data = json.loads(fp.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    return {}
                if "product_stage" in data:
                    from app.models.layers import STAGE_OLD_TO_NEW
                    data["product_stage"] = STAGE_OLD_TO_NEW.get(data["product_stage"], data["product_stage"])
                if "product_level" in data:
                    from app.models.layers import LEVEL_OLD_TO_NEW
                    data["product_level"] = LEVEL_OLD_TO_NEW.get(data["product_level"], data["product_level"])
                return data
            except (json.JSONDecodeError, OSError) as e:
                logger.warning("读取长期配置失败 [%s]: %s", asin, e)
                return {}

    def set_long_term_config(self, asin: str, config: dict) -> bool:
        """写入长期配置，合并现有数据"""
        with self._get_lock(asin):
            self._ensure_asin_dir(asin)
            existing = self.get_long_term_config(asin)
            existing.update(config)
            existing["last_modified"] = datetime.now(timezone.utc).isoformat()
            fp = self._asin_dir(asin) / "long_term_config.json"
            try:
                fp.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
                return True
            except OSError as e:
                logger.error("写入长期配置失败 [%s]: %s", asin, e)
                return False

    def config_exists(self, asin: str) -> bool:
        return (self._asin_dir(asin) / "long_term_config.json").exists()

    # ── 工作流状态 ───────────────────────────────────────

    def get_workflow_state(self, asin: str) -> dict:
        """读取工作流状态"""
        with self._get_lock(asin):
            fp = self._asin_dir(asin) / "workflow_state.json"
            if not fp.exists():
                return {"current_layer": "strategy", "layers_completed": []}
            try:
                data = json.loads(fp.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    return {"current_layer": "strategy", "layers_completed": []}
                return data
            except (json.JSONDecodeError, OSError) as e:
                logger.warning("读取工作流状态失败 [%s]: %s", asin, e)
                return {"current_layer": "strategy", "layers_completed": []}

    def set_workflow_state(self, asin: str, state: dict) -> bool:
        """写入工作流状态"""
        with self._get_lock(asin):
            self._ensure_asin_dir(asin)
            state["last_updated"] = datetime.now(timezone.utc).isoformat()
            fp = self._asin_dir(asin) / "workflow_state.json"
            try:
                fp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
                return True
            except OSError as e:
                logger.error("写入工作流状态失败 [%s]: %s", asin, e)
                return False

    def advance_layer(self, asin: str, layer: str) -> bool:
        """将工作流推进到指定层"""
        with self._get_lock(asin):
            state = self.get_workflow_state(asin)
            completed = state.get("layers_completed", [])
            current = state.get("current_layer", "")
            if current and current not in completed:
                completed.append(current)
            state["layers_completed"] = completed
            state["current_layer"] = layer
            return self.set_workflow_state(asin, state)

    def save_execution(self, asin: str, selection: dict) -> bool:
        """保存执行层选择到工作流状态"""
        with self._get_lock(asin):
            state = self.get_workflow_state(asin)
            state["execution"] = selection
            return self.set_workflow_state(asin, state)

    # ── 调整历史 ─────────────────────────────────────────

    # ── 调整历史（7天窗口，按日期分组）─────────────────────

    def _next_5am_utc(self) -> datetime:
        """计算下一个北京时间 5:00 对应的 UTC 时间"""
        from datetime import timedelta
        now = datetime.now(timezone.utc)
        today_cutoff = now.replace(hour=21, minute=0, second=0, microsecond=0)
        if now >= today_cutoff:
            return today_cutoff + timedelta(days=1)
        return today_cutoff

    def get_adjustment_history(self, asin: str, days: int = 7) -> list[dict]:
        """读取最近 N 天调整记录，自动清理超期数据"""
        with self._get_lock(asin):
            fp = self._asin_dir(asin) / "adjustment_history.json"
            if not fp.exists():
                return []
            try:
                data = json.loads(fp.read_text(encoding="utf-8"))
                records: list = data.get("records", [])
                cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
                valid = [r for r in records if r.get("date", "") >= cutoff]
                if len(valid) < len(records):
                    data["records"] = valid
                    fp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
                return valid
            except (json.JSONDecodeError, OSError) as e:
                logger.warning("读取调整历史失败 [%s]: %s", asin, e)
                return []

    def record_adjustment(self, asin: str, target_acos: int | None = None,
                          daily_budget: float | None = None) -> bool:
        """记录一次调整。按日期分组，同日多次取最新。自动清理超 7 天。"""
        with self._get_lock(asin):
            from datetime import timedelta
            self._ensure_asin_dir(asin)
            fp = self._asin_dir(asin) / "adjustment_history.json"
            existing_data = {}
            if fp.exists():
                try:
                    existing_data = json.loads(fp.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    pass
            records: list = existing_data.get("records", [])
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            now_iso = datetime.now(timezone.utc).isoformat()
            existing = next((r for r in records if r.get("date") == today), None)
            if existing:
                existing["operated_at"] = now_iso
                if target_acos is not None:
                    existing["target_acos"] = target_acos
                if daily_budget is not None:
                    existing["daily_budget"] = daily_budget
            else:
                records.insert(0, {
                    "date": today,
                    **({} if target_acos is None else {"target_acos": target_acos}),
                    **({} if daily_budget is None else {"daily_budget": daily_budget}),
                    "operated_at": now_iso,
                })
            cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d")
            records = [r for r in records if r.get("date", "") >= cutoff]
            existing_data["records"] = records
            try:
                fp.write_text(json.dumps(existing_data, ensure_ascii=False, indent=2), encoding="utf-8")
                return True
            except OSError as e:
                logger.error("写入调整历史失败 [%s]: %s", asin, e)
                return False

    # ── P3 LLM 推荐缓存（每日 5:00 过期）────────────────────

    def get_p3_recommendation(self, asin: str) -> dict | None:
        """读取缓存的 P3 LLM 推荐，过期返回 None"""
        fp = self._asin_dir(asin) / "p3_recommendation.json"
        if not fp.exists():
            return None
        try:
            data = json.loads(fp.read_text(encoding="utf-8"))
            expires_at = datetime.fromisoformat(data.get("expires_at", ""))
            if datetime.now(timezone.utc) > expires_at:
                try:
                    fp.unlink()
                except OSError:
                    pass
                return None
            data["from_cache"] = True
            data["cached_until"] = data.get("expires_at", "")
            return data
        except (json.JSONDecodeError, OSError, ValueError) as e:
            logger.warning("读取P3推荐缓存失败 [%s]: %s", asin, e)
            return None

    def set_p3_recommendation(self, asin: str, result: dict) -> bool:
        """写入 LLM 推荐结果，expires_at = 次日 5:00 BJT"""
        with self._get_lock(asin):
            self._ensure_asin_dir(asin)
            result["created_at"] = datetime.now(timezone.utc).isoformat()
            result["expires_at"] = self._next_5am_utc().isoformat()
            result["from_cache"] = False
            fp = self._asin_dir(asin) / "p3_recommendation.json"
            try:
                fp.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
                return True
            except OSError as e:
                logger.error("写入P3推荐缓存失败 [%s]: %s", asin, e)
                return False

    # ── 目标 ACOS 手动覆盖（每日 5:00 过期）─────────────────

    def get_target_acos_override(self, asin: str) -> int | None:
        """读取目标 ACOS 手动覆盖值，若已过期返回 None"""
        with self._get_lock(asin):
            fp = self._asin_dir(asin) / "target_acos_override.json"
            if not fp.exists():
                return None
            try:
                data = json.loads(fp.read_text(encoding="utf-8"))
                expires_at = datetime.fromisoformat(data.get("expires_at", ""))
                if datetime.now(timezone.utc) > expires_at:
                    try:
                        fp.unlink()
                    except OSError:
                        pass
                    return None
                return data.get("value")
            except (json.JSONDecodeError, OSError, ValueError) as e:
                logger.warning("读取目标ACOS覆盖失败 [%s]: %s", asin, e)
                return None

    def set_target_acos_override(self, asin: str, value: int) -> bool:
        """写入目标 ACOS 手动覆盖，过期时间=次日 5:00（北京时间）"""
        with self._get_lock(asin):
            self._ensure_asin_dir(asin)
            expires_at = self._next_5am_utc()
            fp = self._asin_dir(asin) / "target_acos_override.json"
            try:
                fp.write_text(json.dumps(
                    {"value": value, "expires_at": expires_at.isoformat()},
                    ensure_ascii=False,
                ), encoding="utf-8")
                return True
            except OSError as e:
                logger.error("写入目标ACOS覆盖失败 [%s]: %s", asin, e)
                return False

    def clear_target_acos_override(self, asin: str) -> bool:
        """清除目标 ACOS 手动覆盖"""
        with self._get_lock(asin):
            fp = self._asin_dir(asin) / "target_acos_override.json"
            if fp.exists():
                try:
                    fp.unlink()
                except OSError:
                    pass
        return True
    # ── 反馈日志 ─────────────────────────────────────────

    def _feedback_dir(self, asin: str) -> Path:
        return self._asin_dir(asin) / "feedback"

    def save_feedback(self, submission) -> bool:
        """保存一条反馈日志到独立 JSON 文件"""
        asin = submission.parent_asin
        with self._get_lock(asin):
            feedback_dir = self._feedback_dir(asin)
            feedback_dir.mkdir(parents=True, exist_ok=True)
            fp = feedback_dir / f"{submission.id}.json"
            try:
                fp.write_text(
                    submission.model_dump_json(indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )
                return True
            except OSError as e:
                logger.error("写入反馈日志失败 [%s]: %s", asin, e)
                return False

    def list_feedback(self, asin: str) -> list:
        """列出某 ASIN 的所有反馈记录"""
        from app.models.feedback import FeedbackSubmission
        with self._get_lock(asin):
            feedback_dir = self._feedback_dir(asin)
            if not feedback_dir.exists():
                return []
            items = []
            for fp in sorted(feedback_dir.glob("*.json"), reverse=True):
                try:
                    data = json.loads(fp.read_text(encoding="utf-8"))
                    items.append(FeedbackSubmission(**data))
                except (json.JSONDecodeError, OSError) as e:
                    logger.warning("读取反馈日志失败 [%s]: %s", fp.name, e)
            return items

    def export_feedback(self, asin: str | None = None) -> list[dict]:
        """导出反馈日志为合并 JSON 数组

        asin=None 时导出所有 ASIN 的全部反馈。
        """
        if asin:
            return [item.model_dump() for item in self.list_feedback(asin)]

        all_items = []
        if not self.base_dir.exists():
            return all_items
        for asin_dir in sorted(self.base_dir.iterdir()):
            if not asin_dir.is_dir() or asin_dir.name.startswith("_"):
                continue
            asin = asin_dir.name
            all_items.extend(
                item.model_dump() for item in self.list_feedback(asin)
            )
        return sorted(all_items, key=lambda x: x.get("submitted_at", ""), reverse=True)

    def reset_asin(self, asin: str) -> bool:
        """清除ASIN所有状态（保留长期配置）"""
        fp = self._asin_dir(asin) / "workflow_state.json"
        if fp.exists():
            try:
                fp.unlink()
            except OSError:
                pass
        return self.set_workflow_state(asin, {"current_layer": "strategy", "layers_completed": []})


def _default_state_manager():
    from app.persistence.state_factory import get_state_manager
    return get_state_manager()


# 全局单例（按 STATE_BACKEND 选择 JSON / MySQL）
state_manager = _default_state_manager()

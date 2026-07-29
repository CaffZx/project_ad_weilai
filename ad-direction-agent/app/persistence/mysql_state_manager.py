"""MySQL 长期状态持久化 — 与 JSON StateManager 接口兼容。"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pymysql
from pymysql.cursors import DictCursor

from app.config.settings import settings

logger = logging.getLogger(__name__)
BASE_CONFIG_DIR = Path(__file__).resolve().parent.parent.parent / "config"


def _coerce_shop_id(value) -> int | None:
    try:
        shop_id = int(value)
    except (TypeError, ValueError):
        return None
    return shop_id if shop_id > 0 else None


def _coerce_parent_seller_sku(value) -> str | None:
    sku = str(value or "").strip()
    return sku or None


def _identity_from_mapping(data: dict | None) -> tuple[int | None, str | None]:
    data = data or {}
    shop_id = _coerce_shop_id(
        data.get("shop_id")
        or data.get("_shopId")
        or data.get("shopId")
    )
    parent_seller_sku = _coerce_parent_seller_sku(
        data.get("parent_seller_sku")
        or data.get("_parentSellerSku")
        or data.get("parentSellerSku")
    )
    return shop_id, parent_seller_sku


class MySQLStateManager:
    """ASIN 级长期配置与 AI 结果（MySQL）。"""

    def __init__(self):
        self.base_dir = BASE_CONFIG_DIR
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self._lock_factory = threading.Lock()
        self._locks: dict[str, threading.RLock] = {}
        self._schema_ready = False

    def _connect_kwargs(self) -> dict:
        return {
            "host": settings.state_db_host,
            "port": settings.state_db_port,
            "user": settings.state_db_user,
            "password": settings.state_db_pass,
            "database": settings.state_db_database,
            "charset": "utf8mb4",
            "cursorclass": DictCursor,
            "connect_timeout": 10,
            "read_timeout": 30,
            "write_timeout": 30,
        }

    def _get_lock(self, asin: str) -> threading.RLock:
        with self._lock_factory:
            if asin not in self._locks:
                self._locks[asin] = threading.RLock()
        return self._locks[asin]

    def _execute(self, sql: str, params: tuple = (), fetch: str = "none"):
        conn = pymysql.connect(**self._connect_kwargs())
        try:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                if fetch == "one":
                    return cur.fetchone()
                if fetch == "all":
                    return cur.fetchall()
                conn.commit()
                return None
        finally:
            conn.close()

    async def _run(self, sql: str, params: tuple = (), fetch: str = "none"):
        return await asyncio.to_thread(self._execute, sql, params, fetch)

    def ensure_schema(self) -> None:
        if self._schema_ready:
            return
        schema_path = Path(__file__).parent / "schema.sql"
        if not schema_path.exists():
            raise FileNotFoundError(schema_path)
        raw = schema_path.read_text(encoding="utf-8")
        conn = pymysql.connect(
            host=settings.state_db_host,
            port=settings.state_db_port,
            user=settings.state_db_user,
            password=settings.state_db_pass,
            charset="utf8mb4",
            cursorclass=DictCursor,
            connect_timeout=10,
        )
        try:
            with conn.cursor() as cur:
                for stmt in raw.split(";"):
                    s = stmt.strip()
                    if s and not s.startswith("--"):
                        cur.execute(s)
            conn.commit()
            self._schema_ready = True
            logger.info("MySQL state schema ensured")
        finally:
            conn.close()

    def _next_5am_utc(self) -> datetime:
        now = datetime.now(timezone.utc)
        today_cutoff = now.replace(hour=21, minute=0, second=0, microsecond=0)
        if now >= today_cutoff:
            return today_cutoff + timedelta(days=1)
        return today_cutoff

    def _now(self) -> datetime:
        return datetime.now(timezone.utc)

    # ── 长期配置 ─────────────────────────────────────────

    def get_long_term_config(self, asin: str) -> dict:
        with self._get_lock(asin):
            try:
                self.ensure_schema()
            except Exception as e:  # noqa: BLE001
                logger.warning("schema ensure failed: %s", e)
                return {}
            data: dict = {}

            def _sync_fetch():
                strat = self._execute(
                    "SELECT product_level, product_stage, season_stage, operating_mode, updated_at "
                    "FROM strategy_config WHERE asin=%s",
                    (asin,),
                    "one",
                )
                tact = self._execute(
                    "SELECT ad_purposes, target_keyword_strategy, updated_at FROM tactics_config WHERE asin=%s",
                    (asin,),
                    "one",
                )
                bud = self._execute(
                    "SELECT value, expires_at FROM budget_override WHERE asin=%s",
                    (asin,),
                    "one",
                )
                return strat, tact, bud

            try:
                strat, tact, bud = _sync_fetch()
            except Exception as e:  # noqa: BLE001
                logger.warning("读取长期配置失败 [%s]: %s", asin, e)
                return {}

            if strat:
                data.update({
                    "product_level": strat.get("product_level"),
                    "product_stage": strat.get("product_stage"),
                    "season_stage": strat.get("season_stage"),
                    "operating_mode": strat.get("operating_mode"),
                })
            if tact:
                ap = tact.get("ad_purposes")
                kt = tact.get("target_keyword_strategy")
                if isinstance(ap, str):
                    ap = json.loads(ap)
                if isinstance(kt, str):
                    kt = json.loads(kt)
                data["ad_purposes"] = ap or []
                data["target_keyword_strategy"] = kt or []
            if bud:
                # 人工预算 override 不再按时间过期（2026-06-18 最小改法①）：
                # 失效途径仅 ①「取消覆盖」②保存新值覆盖；new-event 不清除（一向如此），持久继承。
                # expires_at 列保留但读时忽略。与 acos override 对称。
                data["daily_budget_override"] = bud.get("value")
            if "product_stage" in data:
                from app.models.layers import STAGE_OLD_TO_NEW
                data["product_stage"] = STAGE_OLD_TO_NEW.get(
                    data["product_stage"], data["product_stage"]
                )
            if "product_level" in data:
                from app.models.layers import LEVEL_OLD_TO_NEW, normalize_product_level
                pl = normalize_product_level(data["product_level"])
                data["product_level"] = LEVEL_OLD_TO_NEW.get(pl, pl)
            return data

    def set_long_term_config(self, asin: str, config: dict) -> bool:
        with self._get_lock(asin):
            try:
                self.ensure_schema()
            except Exception as e:  # noqa: BLE001
                logger.error("schema ensure failed: %s", e)
                return False
            now = self._now()
            shop_id, parent_seller_sku = _identity_from_mapping(config)
            try:
                if any(k in config for k in ("product_level", "product_stage", "season_stage", "operating_mode")):
                    existing = self.get_long_term_config(asin)
                    pl = config.get("product_level", existing.get("product_level"))
                    ps = config.get("product_stage", existing.get("product_stage"))
                    ss = config.get("season_stage", existing.get("season_stage"))
                    om = config.get("operating_mode", existing.get("operating_mode"))
                    self._execute(
                        "INSERT INTO strategy_config "
                        "(asin, shop_id, parent_seller_sku, product_level, product_stage, season_stage, operating_mode, updated_at) "
                        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE "
                        "shop_id=COALESCE(VALUES(shop_id), shop_id), "
                        "parent_seller_sku=COALESCE(VALUES(parent_seller_sku), parent_seller_sku), "
                        "product_level=VALUES(product_level), product_stage=VALUES(product_stage), "
                        "season_stage=VALUES(season_stage), operating_mode=VALUES(operating_mode), "
                        "updated_at=VALUES(updated_at)",
                        (asin, shop_id, parent_seller_sku, pl, ps, ss, om, now),
                    )
                if "ad_purposes" in config or "target_keyword_strategy" in config:
                    existing = self.get_long_term_config(asin)
                    ap = config.get("ad_purposes", existing.get("ad_purposes", []))
                    kt = config.get("target_keyword_strategy", existing.get("target_keyword_strategy", []))
                    self._execute(
                        "INSERT INTO tactics_config "
                        "(asin, shop_id, parent_seller_sku, ad_purposes, target_keyword_strategy, updated_at) "
                        "VALUES (%s,%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE "
                        "shop_id=COALESCE(VALUES(shop_id), shop_id), "
                        "parent_seller_sku=COALESCE(VALUES(parent_seller_sku), parent_seller_sku), "
                        "ad_purposes=VALUES(ad_purposes), target_keyword_strategy=VALUES(target_keyword_strategy), "
                        "updated_at=VALUES(updated_at)",
                        (
                            asin,
                            shop_id,
                            parent_seller_sku,
                            json.dumps(ap, ensure_ascii=False),
                            json.dumps(kt, ensure_ascii=False),
                            now,
                        ),
                    )
                if "daily_budget_override" in config:
                    val = config["daily_budget_override"]
                    if val is None:
                        self._execute("DELETE FROM budget_override WHERE asin=%s", (asin,))
                    else:
                        exp = self._next_5am_utc()
                        self._execute(
                            "INSERT INTO budget_override "
                            "(asin, shop_id, parent_seller_sku, value, created_at, expires_at) "
                            "VALUES (%s,%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE "
                            "shop_id=COALESCE(VALUES(shop_id), shop_id), "
                            "parent_seller_sku=COALESCE(VALUES(parent_seller_sku), parent_seller_sku), "
                            "value=VALUES(value), created_at=VALUES(created_at), expires_at=VALUES(expires_at)",
                            (asin, shop_id, parent_seller_sku, float(val), now, exp),
                        )
                return True
            except Exception as e:  # noqa: BLE001
                logger.error("写入长期配置失败 [%s]: %s", asin, e)
                return False

    def config_exists(self, asin: str) -> bool:
        try:
            self.ensure_schema()
            row = self._execute("SELECT 1 FROM strategy_config WHERE asin=%s", (asin,), "one")
            return row is not None
        except Exception:
            return False

    # ── 工作流状态 ───────────────────────────────────────

    def _load_workflow_blob(self, asin: str) -> dict:
        wf = {"current_layer": "strategy", "layers_completed": []}
        meta = self._execute(
            "SELECT current_layer, layers_completed, execution_selection FROM workflow_meta WHERE asin=%s",
            (asin,),
            "one",
        )
        if meta:
            wf["current_layer"] = meta.get("current_layer") or "strategy"
            lc = meta.get("layers_completed")
            if isinstance(lc, str):
                lc = json.loads(lc)
            wf["layers_completed"] = lc or []
            ex = meta.get("execution_selection")
            if ex:
                if isinstance(ex, str):
                    ex = json.loads(ex)
                wf["execution"] = ex
        ka_rows = self._execute(
            "SELECT days, payload FROM keyword_analysis WHERE asin=%s", (asin,), "all"
        ) or []
        ts_rows = self._execute(
            "SELECT days, payload FROM target_scores WHERE asin=%s", (asin,), "all"
        ) or []
        if ka_rows:
            ka_map = {}
            for r in ka_rows:
                p = r["payload"]
                if isinstance(p, str):
                    p = json.loads(p)
                ka_map[str(r["days"])] = p
            wf["keyword_analysis"] = ka_map
        if ts_rows:
            ts_map = {}
            for r in ts_rows:
                p = r["payload"]
                if isinstance(p, str):
                    p = json.loads(p)
                ts_map[str(r["days"])] = p
            wf["target_scores"] = ts_map
        return wf

    def get_workflow_state(self, asin: str) -> dict:
        with self._get_lock(asin):
            try:
                self.ensure_schema()
                return self._load_workflow_blob(asin)
            except Exception as e:  # noqa: BLE001
                logger.warning("读取工作流状态失败 [%s]: %s", asin, e)
                return {"current_layer": "strategy", "layers_completed": []}

    def set_workflow_state(self, asin: str, state: dict) -> bool:
        with self._get_lock(asin):
            try:
                self.ensure_schema()
                now = self._now()
                layer = state.get("current_layer", "strategy")
                lc = state.get("layers_completed", [])
                ex = state.get("execution")
                shop_id, parent_seller_sku = _identity_from_mapping(state)
                self._execute(
                    "INSERT INTO workflow_meta "
                    "(asin, shop_id, parent_seller_sku, current_layer, layers_completed, execution_selection, updated_at) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE "
                    "shop_id=COALESCE(VALUES(shop_id), shop_id), "
                    "parent_seller_sku=COALESCE(VALUES(parent_seller_sku), parent_seller_sku), "
                    "current_layer=VALUES(current_layer), layers_completed=VALUES(layers_completed), "
                    "execution_selection=VALUES(execution_selection), updated_at=VALUES(updated_at)",
                    (
                        asin,
                        shop_id,
                        parent_seller_sku,
                        layer,
                        json.dumps(lc, ensure_ascii=False),
                        json.dumps(ex, ensure_ascii=False) if ex else None,
                        now,
                    ),
                )
                ka = state.get("keyword_analysis")
                if isinstance(ka, dict):
                    for days_key, payload in ka.items():
                        days = int(days_key)
                        self._execute(
                            "INSERT INTO keyword_analysis "
                            "(asin, shop_id, parent_seller_sku, days, payload, updated_at) "
                            "VALUES (%s,%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE "
                            "shop_id=COALESCE(VALUES(shop_id), shop_id), "
                            "parent_seller_sku=COALESCE(VALUES(parent_seller_sku), parent_seller_sku), "
                            "payload=VALUES(payload), updated_at=VALUES(updated_at)",
                            (asin, shop_id, parent_seller_sku, days, json.dumps(payload, ensure_ascii=False), now),
                        )
                elif isinstance(ka, list):
                    self._execute(
                        "INSERT INTO keyword_analysis "
                        "(asin, shop_id, parent_seller_sku, days, payload, updated_at) "
                        "VALUES (%s,%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE "
                        "shop_id=COALESCE(VALUES(shop_id), shop_id), "
                        "parent_seller_sku=COALESCE(VALUES(parent_seller_sku), parent_seller_sku), "
                        "payload=VALUES(payload), updated_at=VALUES(updated_at)",
                        (asin, shop_id, parent_seller_sku, 7, json.dumps(ka, ensure_ascii=False), now),
                    )
                ts = state.get("target_scores")
                if isinstance(ts, dict):
                    for days_key, payload in ts.items():
                        days = int(days_key)
                        self._execute(
                            "INSERT INTO target_scores "
                            "(asin, shop_id, parent_seller_sku, days, payload, updated_at) "
                            "VALUES (%s,%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE "
                            "shop_id=COALESCE(VALUES(shop_id), shop_id), "
                            "parent_seller_sku=COALESCE(VALUES(parent_seller_sku), parent_seller_sku), "
                            "payload=VALUES(payload), updated_at=VALUES(updated_at)",
                            (asin, shop_id, parent_seller_sku, days, json.dumps(payload, ensure_ascii=False), now),
                        )
                elif isinstance(ts, list) and ts:
                    self._execute(
                        "INSERT INTO target_scores "
                        "(asin, shop_id, parent_seller_sku, days, payload, updated_at) "
                        "VALUES (%s,%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE "
                        "shop_id=COALESCE(VALUES(shop_id), shop_id), "
                        "parent_seller_sku=COALESCE(VALUES(parent_seller_sku), parent_seller_sku), "
                        "payload=VALUES(payload), updated_at=VALUES(updated_at)",
                        (asin, shop_id, parent_seller_sku, 7, json.dumps(ts, ensure_ascii=False), now),
                    )
                return True
            except Exception as e:  # noqa: BLE001
                logger.error("写入工作流状态失败 [%s]: %s", asin, e)
                return False

    def advance_layer(
        self,
        asin: str,
        layer: str,
        shop_id: int | None = None,
        parent_seller_sku: str | None = None,
    ) -> bool:
        with self._get_lock(asin):
            state = self.get_workflow_state(asin)
            completed = state.get("layers_completed", [])
            current = state.get("current_layer", "")
            if current and current not in completed:
                completed.append(current)
            state["layers_completed"] = completed
            state["current_layer"] = layer
            if shop_id:
                state["shop_id"] = shop_id
            if parent_seller_sku:
                state["parent_seller_sku"] = parent_seller_sku
            return self.set_workflow_state(asin, state)

    def save_execution(
        self,
        asin: str,
        selection: dict,
        shop_id: int | None = None,
        parent_seller_sku: str | None = None,
    ) -> bool:
        with self._get_lock(asin):
            state = self.get_workflow_state(asin)
            state["execution"] = selection
            if shop_id:
                state["shop_id"] = shop_id
            if parent_seller_sku:
                state["parent_seller_sku"] = parent_seller_sku
            return self.set_workflow_state(asin, state)

    def get_adjustment_history(self, asin: str, days: int = 7) -> list[dict]:
        with self._get_lock(asin):
            try:
                self.ensure_schema()
                cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).date()
                rows = self._execute(
                    "SELECT record_date AS date, target_acos, daily_budget, operated_at "
                    "FROM adjustment_history WHERE asin=%s AND record_date>=%s "
                    "ORDER BY record_date DESC",
                    (asin, cutoff),
                    "all",
                ) or []
                out = []
                for r in rows:
                    d = r.get("date")
                    out.append({
                        "date": d.isoformat() if hasattr(d, "isoformat") else str(d),
                        "target_acos": r.get("target_acos"),
                        "daily_budget": r.get("daily_budget"),
                        "operated_at": r.get("operated_at").isoformat()
                        if hasattr(r.get("operated_at"), "isoformat")
                        else r.get("operated_at"),
                    })
                self._execute(
                    "DELETE FROM adjustment_history WHERE asin=%s AND record_date<%s",
                    (asin, cutoff),
                )
                return out
            except Exception as e:  # noqa: BLE001
                logger.warning("读取调整历史失败 [%s]: %s", asin, e)
                return []

    def record_adjustment(self, asin: str, target_acos: int | None = None,
                          daily_budget: float | None = None,
                          shop_id: int | None = None,
                          parent_seller_sku: str | None = None) -> bool:
        with self._get_lock(asin):
            try:
                self.ensure_schema()
                today = datetime.now(timezone.utc).date()
                now = self._now()
                shop_id = _coerce_shop_id(shop_id)
                parent_seller_sku = _coerce_parent_seller_sku(parent_seller_sku)
                self._execute(
                    "INSERT INTO adjustment_history "
                    "(asin, shop_id, parent_seller_sku, record_date, target_acos, daily_budget, operated_at) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE "
                    "shop_id=COALESCE(VALUES(shop_id), shop_id), "
                    "parent_seller_sku=COALESCE(VALUES(parent_seller_sku), parent_seller_sku), "
                    "target_acos=COALESCE(VALUES(target_acos), target_acos), "
                    "daily_budget=COALESCE(VALUES(daily_budget), daily_budget), "
                    "operated_at=VALUES(operated_at)",
                    (asin, shop_id, parent_seller_sku, today, target_acos, daily_budget, now),
                )
                cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).date()
                self._execute(
                    "DELETE FROM adjustment_history WHERE asin=%s AND record_date<%s",
                    (asin, cutoff),
                )
                return True
            except Exception as e:  # noqa: BLE001
                logger.error("写入调整历史失败 [%s]: %s", asin, e)
                return False

    def get_p3_recommendation(self, asin: str) -> dict | None:
        try:
            self.ensure_schema()
            row = self._execute(
                "SELECT payload, expires_at FROM p3_recommendation WHERE asin=%s",
                (asin,),
                "one",
            )
            if not row:
                return None
            exp = row["expires_at"]
            if isinstance(exp, str):
                exp = datetime.fromisoformat(exp)
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) > exp:
                self._execute("DELETE FROM p3_recommendation WHERE asin=%s", (asin,))
                return None
            payload = row["payload"]
            if isinstance(payload, str):
                data = json.loads(payload)
            else:
                data = payload
            data["from_cache"] = True
            data["cached_until"] = exp.isoformat()
            return data
        except Exception as e:  # noqa: BLE001
            logger.warning("读取P3推荐缓存失败 [%s]: %s", asin, e)
            return None

    def set_p3_recommendation(self, asin: str, result: dict) -> bool:
        with self._get_lock(asin):
            try:
                self.ensure_schema()
                now = self._now()
                exp = self._next_5am_utc()
                result = dict(result)
                result["created_at"] = now.isoformat()
                result["expires_at"] = exp.isoformat()
                result["from_cache"] = False
                shop_id, parent_seller_sku = _identity_from_mapping(result)
                self._execute(
                    "INSERT INTO p3_recommendation "
                    "(asin, shop_id, parent_seller_sku, payload, created_at, expires_at) "
                    "VALUES (%s,%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE "
                    "shop_id=COALESCE(VALUES(shop_id), shop_id), "
                    "parent_seller_sku=COALESCE(VALUES(parent_seller_sku), parent_seller_sku), "
                    "payload=VALUES(payload), created_at=VALUES(created_at), expires_at=VALUES(expires_at)",
                    (asin, shop_id, parent_seller_sku, json.dumps(result, ensure_ascii=False), now, exp),
                )
                return True
            except Exception as e:  # noqa: BLE001
                logger.error("写入P3推荐缓存失败 [%s]: %s", asin, e)
                return False

    def get_target_acos_override(self, asin: str) -> int | None:
        with self._get_lock(asin):
            try:
                self.ensure_schema()
                row = self._execute(
                    "SELECT value, expires_at FROM acos_override WHERE asin=%s",
                    (asin,),
                    "one",
                )
                if not row:
                    return None
                # 人工 ACOS override 不再按时间过期（2026-06-18 最小改法①）：
                # 失效途径仅两条 —— ①运营点「取消覆盖」(clear_target_acos_override) ②保存新值覆盖。
                # new-event 已不再清除它（③，与预算对齐），持久继承；expires_at 列保留但读时忽略。
                return int(row["value"])
            except Exception as e:  # noqa: BLE001
                logger.warning("读取目标ACOS覆盖失败 [%s]: %s", asin, e)
                return None

    def set_target_acos_override(
        self,
        asin: str,
        value: int,
        shop_id: int | None = None,
        parent_seller_sku: str | None = None,
    ) -> bool:
        with self._get_lock(asin):
            try:
                self.ensure_schema()
                now = self._now()
                exp = self._next_5am_utc()
                shop_id = _coerce_shop_id(shop_id)
                parent_seller_sku = _coerce_parent_seller_sku(parent_seller_sku)
                self._execute(
                    "INSERT INTO acos_override "
                    "(asin, shop_id, parent_seller_sku, value, created_at, expires_at) "
                    "VALUES (%s,%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE "
                    "shop_id=COALESCE(VALUES(shop_id), shop_id), "
                    "parent_seller_sku=COALESCE(VALUES(parent_seller_sku), parent_seller_sku), "
                    "value=VALUES(value), created_at=VALUES(created_at), expires_at=VALUES(expires_at)",
                    (asin, shop_id, parent_seller_sku, value, now, exp),
                )
                return True
            except Exception as e:  # noqa: BLE001
                logger.error("写入目标ACOS覆盖失败 [%s]: %s", asin, e)
                return False

    def clear_target_acos_override(self, asin: str) -> bool:
        with self._get_lock(asin):
            try:
                self.ensure_schema()
                self._execute("DELETE FROM acos_override WHERE asin=%s", (asin,))
            except Exception:
                pass
        return True

    def clear_p3_recommendation(self, asin: str) -> bool:
        """清 P3 缓存（新建事件时重做 3）。"""
        with self._get_lock(asin):
            try:
                self.ensure_schema()
                self._execute("DELETE FROM p3_recommendation WHERE asin=%s", (asin,))
            except Exception:
                pass
        return True

    # ── 进行中分析事件（run_id 作批次句柄）─────────────────

    def get_analysis_session(self, asin: str) -> dict | None:
        try:
            self.ensure_schema()
            row = self._execute(
                "SELECT run_id, started_at, execution_started_at FROM analysis_session WHERE asin=%s", (asin,), "one"
            )
            if not row or not row.get("run_id"):
                return None
            st = row.get("started_at")
            ex_st = row.get("execution_started_at")
            # MySQL datetime 列读出来是 naive；self._now() 是 aware UTC
            # （set_analysis_session 写入时也是 _now() 的 UTC 值），
            # 比较时显式补 tzinfo，避免 "can't subtract offset-naive and offset-aware datetimes"。
            if st and st.tzinfo is None:
                st = st.replace(tzinfo=timezone.utc)
            if ex_st and ex_st.tzinfo is None:
                ex_st = ex_st.replace(tzinfo=timezone.utc)
            # TTL 12h：超期自动清，防止运营执行权永久冻结
            if st and (self._now() - st).total_seconds() > 12 * 3600:
                self.clear_analysis_session(asin)
                return None
            return {"run_id": row["run_id"],
                    "started_at": st.isoformat() if hasattr(st, "isoformat") else st,
                    "execution_started_at": ex_st.isoformat() if hasattr(ex_st, "isoformat") else ex_st}
        except Exception as e:  # noqa: BLE001
            logger.warning("读取分析事件标记失败 [%s]: %s", asin, e)
            return None

    def set_analysis_session(
        self,
        asin: str,
        run_id: str,
        shop_id: int | None = None,
        parent_seller_sku: str | None = None,
    ) -> bool:
        with self._get_lock(asin):
            try:
                self.ensure_schema()
                shop_id = _coerce_shop_id(shop_id)
                parent_seller_sku = _coerce_parent_seller_sku(parent_seller_sku)
                self._execute(
                    "INSERT INTO analysis_session "
                    "(asin, shop_id, parent_seller_sku, run_id, started_at) "
                    "VALUES (%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE "
                    "shop_id=COALESCE(VALUES(shop_id), shop_id), "
                    "parent_seller_sku=COALESCE(VALUES(parent_seller_sku), parent_seller_sku), "
                    "run_id=VALUES(run_id), started_at=VALUES(started_at)",
                    (asin, shop_id, parent_seller_sku, run_id, self._now()),
                )
                return True
            except Exception as e:  # noqa: BLE001
                logger.error("写入分析事件标记失败 [%s]: %s", asin, e)
                return False

    def mark_analysis_execution_started(
        self,
        asin: str,
        run_id: str,
        shop_id: int | None = None,
        parent_seller_sku: str | None = None,
    ) -> bool:
        with self._get_lock(asin):
            try:
                self.ensure_schema()
                shop_id = _coerce_shop_id(shop_id)
                parent_seller_sku = _coerce_parent_seller_sku(parent_seller_sku)
                conn = pymysql.connect(**self._connect_kwargs())
                try:
                    with conn.cursor() as cur:
                        affected = cur.execute(
                            "UPDATE analysis_session SET execution_started_at=%s, "
                            "shop_id=COALESCE(%s, shop_id), "
                            "parent_seller_sku=COALESCE(%s, parent_seller_sku) "
                            "WHERE asin=%s AND run_id=%s",
                            (self._now(), shop_id, parent_seller_sku, asin, run_id),
                        )
                    conn.commit()
                    return bool(affected)
                finally:
                    conn.close()
            except Exception as e:  # noqa: BLE001
                logger.error("写入执行层分析启动标记失败 [%s]: %s", asin, e)
                return False

    def clear_analysis_execution_started(self, asin: str) -> bool:
        with self._get_lock(asin):
            try:
                self.ensure_schema()
                self._execute(
                    "UPDATE analysis_session SET execution_started_at=NULL WHERE asin=%s",
                    (asin,),
                )
                return True
            except Exception as e:  # noqa: BLE001
                logger.warning("清除执行层分析启动标记失败 [%s]: %s", asin, e)
                return False

    def clear_analysis_session(self, asin: str) -> bool:
        with self._get_lock(asin):
            try:
                self.ensure_schema()
                self._execute("DELETE FROM analysis_session WHERE asin=%s", (asin,))
            except Exception as exc:  # noqa: BLE001
                logger.warning("清除分析事件失败 [%s]: %s", asin, exc)
                return False
        return True

    def save_feedback(self, submission) -> bool:
        asin = submission.parent_asin
        with self._get_lock(asin):
            try:
                self.ensure_schema()
                payload = submission.model_dump()
                shop_id, parent_seller_sku = _identity_from_mapping(payload)
                submitted = payload.get("submitted_at")
                if submitted and isinstance(submitted, str):
                    submitted_dt = datetime.fromisoformat(submitted.replace("Z", "+00:00"))
                else:
                    submitted_dt = self._now()
                self._execute(
                    "INSERT INTO feedback (id, asin, shop_id, parent_seller_sku, payload, submitted_at) "
                    "VALUES (%s,%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE "
                    "shop_id=COALESCE(VALUES(shop_id), shop_id), "
                    "parent_seller_sku=COALESCE(VALUES(parent_seller_sku), parent_seller_sku), "
                    "payload=VALUES(payload), submitted_at=VALUES(submitted_at)",
                    (
                        submission.id,
                        asin,
                        shop_id,
                        parent_seller_sku,
                        json.dumps(payload, ensure_ascii=False),
                        submitted_dt,
                    ),
                )
                return True
            except Exception as e:  # noqa: BLE001
                logger.error("写入反馈日志失败 [%s]: %s", asin, e)
                return False

    def list_feedback(self, asin: str) -> list:
        from app.models.feedback import FeedbackSubmission

        with self._get_lock(asin):
            try:
                self.ensure_schema()
                rows = self._execute(
                    "SELECT payload FROM feedback WHERE asin=%s ORDER BY submitted_at DESC",
                    (asin,),
                    "all",
                ) or []
                items = []
                for r in rows:
                    p = r["payload"]
                    if isinstance(p, str):
                        p = json.loads(p)
                    items.append(FeedbackSubmission(**p))
                return items
            except Exception as e:  # noqa: BLE001
                logger.warning("读取反馈失败 [%s]: %s", asin, e)
                return []

    def export_feedback(self, asin: str | None = None) -> list[dict]:
        if asin:
            return [item.model_dump() for item in self.list_feedback(asin)]
        try:
            self.ensure_schema()
            rows = self._execute(
                "SELECT payload FROM feedback ORDER BY submitted_at DESC", (), "all"
            ) or []
            out = []
            for r in rows:
                p = r["payload"]
                if isinstance(p, str):
                    p = json.loads(p)
                out.append(p)
            return out
        except Exception:
            return []

    def reset_asin(self, asin: str) -> bool:
        with self._get_lock(asin):
            try:
                self.ensure_schema()
                self._execute("DELETE FROM keyword_analysis WHERE asin=%s", (asin,))
                self._execute("DELETE FROM target_scores WHERE asin=%s", (asin,))
            except Exception:
                pass
        return self.set_workflow_state(asin, {"current_layer": "strategy", "layers_completed": []})

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import logging
from typing import Any, TYPE_CHECKING

import pymysql
from pymysql.cursors import DictCursor

from app.config.settings import settings
from app.core.core_keyword_policy import (
    normalize_core_keyword,
    resolve_effective_core_keywords,
)
from app.workflow.steps.campaign_portfolio import is_strictly_in_low_bid_pool
from .erp_display import build_whip_display_fields
from .models import CanonicalRun, stable_id, warehouse_pending_id
from .text_utils import (
    build_wizard_direction_content_json,
    json_dumps,
    level_to_score,
    map_direction_type,
    map_direction_types_json,
    map_product_position,
    map_product_stage,
    map_purpose_target,
    map_season_type,
    map_target_keyword_type,
    split_reason_sections,
    to_enum_list,
)

if TYPE_CHECKING:
    from app.models.campaign import CampaignUnit

logger = logging.getLogger(__name__)


CORE_KEYWORD_POLICY_STATES = frozenset({"LOCKED", "ENABLED", "DISABLED", "VETOED"})


def core_keyword_task_version(value: datetime | str | None) -> str:
    """统一 DB datetime 与 API ISO datetime，作为管理写入的乐观锁版本。"""
    if value is None:
        return ""
    return str(value).replace(" ", "T", 1)


def _audit_int(v: Any) -> int | None:
    """审计列 create_by/editor_by(int) / creator_id/editor_id(bigint) 需数值；
    operator 可能是非数字串（如 'tab5'）→ 返回 None 入 NULL，数字串→int。"""
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


@dataclass(slots=True)
class WriteReport:
    decision_id: str
    decision: int = 0
    decision_config: int = 0
    modern_summary: int = 0
    modern_card: int = 0
    modern_keyword_pending: int = 0
    modern_campaign_pending: int = 0
    modern_placement_pending: int = 0
    legacy_recommend: int = 0
    legacy_detail: int = 0
    legacy_metrics: int = 0
    purpose_score: int = 0
    core_keyword_tracking: int = 0
    ai_suggest: int = 0
    direction_recommend: int = 0
    direction_detail: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "decision_id": self.decision_id,
            "decision": self.decision,
            "decision_config": self.decision_config,
            "modern_summary": self.modern_summary,
            "modern_card": self.modern_card,
            "modern_keyword_pending": self.modern_keyword_pending,
            "modern_campaign_pending": self.modern_campaign_pending,
            "modern_placement_pending": self.modern_placement_pending,
            "legacy_recommend": self.legacy_recommend,
            "legacy_detail": self.legacy_detail,
            "legacy_metrics": self.legacy_metrics,
            "purpose_score": self.purpose_score,
            "core_keyword_tracking": self.core_keyword_tracking,
            "ai_suggest": self.ai_suggest,
            "direction_recommend": self.direction_recommend,
            "direction_detail": self.direction_detail,
        }


class ErpDualWriterRepository:
    def __init__(self, *, host: str, port: int, user: str, password: str,
                 database: str, use_tls: bool = False):
        self._conn_kwargs = {
            "host": host,
            "port": port,
            "user": user,
            "password": password,
            "database": database,
            "charset": "utf8mb4",
            "cursorclass": DictCursor,
            "connect_timeout": 10,
            "read_timeout": 30,
            "write_timeout": 30,
            "autocommit": False,
        }
        if use_tls:
            # caching_sha2_password 经非 LAN（本机/VPN）需 TLS 握手；CERT_NONE 不校验证书即可
            import ssl as _ssl
            ctx = _ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = _ssl.CERT_NONE
            self._conn_kwargs["ssl"] = ctx

    def _connect(self):
        return pymysql.connect(**self._conn_kwargs)

    # ── 批次生命周期（决策批次改造方案 §一） ──────────────────────────────

    # 注：批次 = 一行 t_advert_agent_decision，id=decision_id 写库时生成（不预建 DRAFT 行）。
    #     "进行中"态用 run_id 落 state 库（StateManager.*_analysis_session），不污染 ERP 库。
    #     decision 表无 decision_status 列；所有 decision 行即"已完成批次"。

    def list_batches(self, asin: str) -> list[dict]:
        """返回该 ASIN 所有已完成决策批次，按 create_time 降序。"""
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT id AS decision_id, analysis_mode, is_latest, batch_no,
                              create_time AS completed_at
                       FROM t_advert_agent_decision
                       WHERE parent_asin = %s
                       ORDER BY create_time DESC""",
                    (asin,),
                )
                return cur.fetchall()
        finally:
            conn.close()

    def get_latest_completed(self, asin: str) -> dict | None:
        """返回 is_latest=1 的最新批次（is_latest 由 finalize_batch 维护）。"""
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT id AS decision_id, analysis_mode, is_latest, batch_no,
                              create_time AS completed_at
                       FROM t_advert_agent_decision
                       WHERE parent_asin = %s AND is_latest = 1
                       ORDER BY create_time DESC LIMIT 1""",
                    (asin,),
                )
                return cur.fetchone()
        finally:
            conn.close()

    # ── 执行层快照回读（P2: snapshot → ViewModel 源） ──────────────────────

    def read_snapshot(self, decision_id: str) -> dict | None:
        """读取某决策批次的执行层快照（decision + summary + cards + 3×pending）。

        返回 raw dict 供 campaign_viewmodel.from_db_snapshot 组装。SELECT * 容错:
        新列（keyword_class/is_prefiltered/perf_json 等）未迁移时自然缺省。
        """
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM t_advert_agent_decision WHERE id=%s", (decision_id,)
                )
                decision = cur.fetchone()
                if not decision:
                    return None
                cur.execute(
                    "SELECT * FROM t_advert_agent_modify_suggest_summary WHERE decision_id=%s",
                    (decision_id,),
                )
                summary = cur.fetchone()
                cur.execute(
                    "SELECT * FROM t_advert_agent_modify_suggest_card "
                    "WHERE decision_id=%s ORDER BY sort_order, create_time",
                    (decision_id,),
                )
                cards = cur.fetchall() or []
                cur.execute(
                    "SELECT * FROM t_advert_agent_modify_campaign_pending WHERE decision_id=%s",
                    (decision_id,),
                )
                campaign_pending = cur.fetchall() or []
                cur.execute(
                    "SELECT * FROM t_advert_agent_modify_keyword_pending WHERE decision_id=%s",
                    (decision_id,),
                )
                keyword_pending = cur.fetchall() or []
                cur.execute(
                    "SELECT * FROM t_advert_agent_modify_placement_pending WHERE decision_id=%s",
                    (decision_id,),
                )
                placement_pending = cur.fetchall() or []
                cur.execute(
                    "SELECT * FROM t_advert_agent_modify_suggest_reason_group "
                    "WHERE decision_id=%s ORDER BY sort_order",
                    (decision_id,),
                )
                reason_groups = cur.fetchall() or []
                cur.execute(
                    "SELECT m.* FROM t_advert_agent_modify_suggest_reason_group_member m "
                    "JOIN t_advert_agent_modify_suggest_reason_group g ON m.group_id=g.id "
                    "WHERE g.decision_id=%s ORDER BY m.sort_order",
                    (decision_id,),
                )
                reason_members = cur.fetchall() or []
                cur.execute(
                    "SELECT * FROM t_advert_agent_modify_suggest_special "
                    "WHERE decision_id=%s ORDER BY sort_order",
                    (decision_id,),
                )
                specials = cur.fetchall() or []
            return {
                "decision": decision,
                "summary": summary,
                "cards": cards,
                "campaign_pending": campaign_pending,
                "keyword_pending": keyword_pending,
                "placement_pending": placement_pending,
                "reason_groups": reason_groups,
                "reason_members": reason_members,
                "specials": specials,
            }
        finally:
            conn.close()

    def confirm_decisions(
        self, decision_id: str, decisions: list[dict], operator: str | None = None,
        *, in_progress: bool = False,
    ) -> dict:
        """运营审核写回 card + 子 pending 的 confirm_status（PENDING→CONFIRMED/REJECTED）。

        - 校验可执行：批次 is_latest=1 且该 ASIN 无进行中事件（in_progress 由 API 层查 state 库传入）。
        - 每活动只处理一次：UPDATE 限 confirm_status='PENDING'，重复处理 rowcount=0 → skipped。
        decisions: [{campaign_key: <card_id>, decision: 'approve'|'reject'}]
        """
        conn = self._connect()
        now = datetime.now()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT parent_asin, is_latest FROM t_advert_agent_decision WHERE id=%s",
                    (decision_id,),
                )
                d = cur.fetchone()
                if not d:
                    return {"ok": False, "error": "decision 不存在", "applied": 0, "skipped": 0}
                if not d.get("is_latest"):
                    return {"ok": False, "error": "该批次非最新批次，不可执行",
                            "applied": 0, "skipped": 0}
                if in_progress:
                    return {"ok": False, "error": "存在进行中分析事件，执行权已冻结",
                            "applied": 0, "skipped": 0}

                applied = 0
                skipped = 0
                for item in decisions:
                    card_id = str(item.get("campaign_key") or item.get("card_id") or "").strip()
                    raw = str(item.get("decision") or "").lower()
                    if not card_id or raw not in ("approve", "reject"):
                        continue
                    status = "CONFIRMED" if raw == "approve" else "REJECTED"
                    cur.execute(
                        "UPDATE t_advert_agent_modify_suggest_card "
                        "SET confirm_status=%s, confirm_user_id=%s, confirm_time=%s, update_time=%s "
                        "WHERE id=%s AND decision_id=%s AND confirm_status='PENDING'",
                        (status, operator, now, now, card_id, decision_id),
                    )
                    if cur.rowcount == 0:
                        skipped += 1  # 已处理过 / 不存在
                        continue
                    applied += 1
                    for table in (
                        "t_advert_agent_modify_keyword_pending",
                        "t_advert_agent_modify_campaign_pending",
                        "t_advert_agent_modify_placement_pending",
                    ):
                        cur.execute(
                            f"UPDATE {table} SET confirm_status=%s, confirm_user_id=%s, "
                            "confirm_time=%s, update_time=%s "
                            "WHERE suggest_card_id=%s AND confirm_status='PENDING'",
                            (status, operator, now, now, card_id),
                        )
            conn.commit()
            return {"ok": True, "applied": applied, "skipped": skipped}
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ── 广告调整 MCP 真实执行（Part 6） ─────────────────────────────────────
    # 链路：confirm(标 CONFIRMED) → load_confirmed_pending → mapper → 调 MCP →
    #       insert_advert_record(主) + insert_*_record(子) + update_pending_execute_status

    def load_confirmed_pending(self, decision_id: str) -> dict | None:
        """读取某批次 CONFIRMED 且 execute_status=PENDING 的待执行 pending 行 + cards。

        仅取已确认(approve)且未执行的行 → 天然幂等（已执行的 execute_status≠PENDING 不再取）。
        """
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, shop_id, parent_asin, parent_seller_sku, site_code, batch_no "
                    "FROM t_advert_agent_decision WHERE id=%s",
                    (decision_id,),
                )
                decision = cur.fetchone()
                if not decision:
                    return None
                cur.execute(
                    "SELECT id, campaign_id, campaign_name, suggest_category, "
                    "campaign_group_type, keyword_match_type, asin, keyword, "
                    "perf_json, trigger_rule "
                    "FROM t_advert_agent_modify_suggest_card "
                    "WHERE decision_id=%s AND confirm_status='CONFIRMED'",
                    (decision_id,),
                )
                cards = cur.fetchall() or []
                rows: dict[str, list] = {}
                for table, key in (
                    ("t_advert_agent_modify_campaign_pending", "campaign_pending"),
                    ("t_advert_agent_modify_keyword_pending", "keyword_pending"),
                    ("t_advert_agent_modify_placement_pending", "placement_pending"),
                ):
                    cur.execute(
                        f"SELECT * FROM {table} WHERE decision_id=%s "
                        "AND confirm_status='CONFIRMED' AND execute_status='PENDING'",
                        (decision_id,),
                    )
                    rows[key] = cur.fetchall() or []
            return {"decision": decision, "cards": cards, **rows}
        finally:
            conn.close()

    def load_pending_by_card_ids(self, decision_id: str, card_ids: list[str]) -> dict | None:
        """按 card_id 拉 cards + pending（不要求 confirm_status='CONFIRMED'）。

        供"合并审核+执行"流程：前端选中即跑，不写任何 confirm_status / record。
        """
        if not card_ids:
            return None
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, shop_id, parent_asin, parent_seller_sku, site_code, batch_no "
                    "FROM t_advert_agent_decision WHERE id=%s",
                    (decision_id,),
                )
                decision = cur.fetchone()
                if not decision:
                    return None
                ph = ",".join(["%s"]*len(card_ids))
                cur.execute(
                    "SELECT id, campaign_id, campaign_name, suggest_category, "
                    "campaign_group_type, keyword_match_type, asin, keyword, "
                    "perf_json, trigger_rule "
                    f"FROM t_advert_agent_modify_suggest_card "
                    f"WHERE decision_id=%s AND id IN ({ph})",
                    (decision_id, *card_ids),
                )
                cards = cur.fetchall() or []
                rows: dict[str, list] = {}
                for table, key in (
                    ("t_advert_agent_modify_campaign_pending", "campaign_pending"),
                    ("t_advert_agent_modify_keyword_pending", "keyword_pending"),
                    ("t_advert_agent_modify_placement_pending", "placement_pending"),
                ):
                    cur.execute(
                        f"SELECT * FROM {table} WHERE decision_id=%s "
                        f"AND suggest_card_id IN ({ph}) AND execute_status='PENDING'",
                        (decision_id, *card_ids),
                    )
                    rows[key] = cur.fetchall() or []
            return {"decision": decision, "cards": cards, **rows}
        finally:
            conn.close()

    def get_decision_basic(self, decision_id: str) -> dict | None:
        """读取批次基础上下文（shop_id/parent_asin/parent_seller_sku 等）。供组合预算执行。"""
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, shop_id, parent_asin, parent_seller_sku, site_code, batch_no "
                    "FROM t_advert_agent_decision WHERE id=%s",
                    (decision_id,),
                )
                return cur.fetchone()
        finally:
            conn.close()

    def insert_advert_record(
        self, *, decision_id: str, shop_id: int, parent_asin: str,
        parent_seller_sku: str, current_user_id: str, request_params_json: str,
        task_id: str = "", response_params_json: str = "",
    ) -> str:
        # 不写 t_advert_agent_modify_advert_record（操作记录由 ERP 系统自身维护）
        return stable_id("aer", decision_id, datetime.now().isoformat())


    def update_advert_record_result(
        self, record_id: str, *, task_id: str = "", response_params_json: str = "",
    ) -> None:
        # 不更新 t_advert_agent_modify_advert_record（操作记录由 ERP 系统自身维护）
        return None


    def insert_exec_sub_records(self, record_id: str, ops: list[dict], operator: str) -> int:
        # 不写 t_advert_agent_modify_{campaign,keyword,placement}_record（操作记录由 ERP 系统自身维护）
        return 0


    def insert_portfolio_records(
        self, record_id: str, ops: list[dict], operator: str, *,
        shop_id: int = 0, parent_asin: str = "", parent_seller_sku: str = "",
    ) -> int:
        # 不写 t_advert_agent_modify_portfolio_record（操作记录由 ERP 系统自身维护）
        return 0


    def update_pending_execute_status(
        self, ops: list[dict], status: str, *, operator: str = "", msg: str = "",
    ) -> None:
        """按 ops 的 pending_id 回写各 pending 表 execute_status / execute_msg / execute_time。"""
        by_table = {
            "campaign": "t_advert_agent_modify_campaign_pending",
            "keyword": "t_advert_agent_modify_keyword_pending",
            "placement": "t_advert_agent_modify_placement_pending",
        }
        conn = self._connect()
        now = datetime.now()
        try:
            with conn.cursor() as cur:
                for op in ops:
                    pid = op.get("pending_id")
                    table = by_table.get(op.get("record_kind"))
                    if not pid or not table:
                        continue
                    st = op.get("execute_status") or status
                    em = op.get("error_msg") or msg or None
                    cur.execute(
                        f"UPDATE {table} SET execute_status=%s, execute_msg=%s, "
                        "execute_time=%s, update_time=%s WHERE id=%s",
                        (st, em, now, now, pid),
                    )
            conn.commit()
        finally:
            conn.close()

    def get_decision_preset(self, decision_id: str) -> dict | None:
        """读取某批次冻结的前置 1-4 配置（decision 行的策略/策略/P3/方向列）。"""
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, parent_asin, parent_seller_sku, shop_id, site_code, day_range, "
                    "product_position, product_stage, season_type, advert_purposes, "
                    "target_keyword_types, target_acos_suggest, daily_budget_suggest, "
                    "advert_direction_types, analysis_mode, is_latest, "
                    "batch_no, create_time, update_time "
                    "FROM t_advert_agent_decision WHERE id=%s",
                    (decision_id,),
                )
                return cur.fetchone()
        finally:
            conn.close()

    def read_decision_preset_rich(self, decision_id: str) -> dict | None:
        """前置 1-4 富快照：decision 主行 + purpose_score(Tab1) + ai_suggest(Tab3)。

        数据由 write_full 的 _upsert_purpose_scores / _upsert_ai_suggest 落库（零额外写）。
        子表读各自 try 包裹：表/段缺失（未跑前置 AI、ERP 未迁移）时该段降级为空，
        不连累 decision 主行返回。Tab2(data_metrics)/Tab4(direction_detail) 待后续接入。
        """
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM t_advert_agent_decision WHERE id=%s", (decision_id,)
                )
                decision = cur.fetchone()
                if not decision:
                    return None

                purpose_scores: list = []
                try:
                    cur.execute(
                        "SELECT advert_purpose, score, recommend_level, decision_basis, "
                        "suggest, future_attention FROM t_advert_agent_purpose_score "
                        "WHERE decision_id=%s ORDER BY create_time",
                        (decision_id,),
                    )
                    purpose_scores = cur.fetchall() or []
                except Exception as e:  # noqa: BLE001
                    logger.warning("read purpose_score 降级 [%s]: %s", decision_id, e)

                ai_suggest = None
                try:
                    cur.execute(
                        "SELECT suggest_acos, suggest_acos_tag, suggest_acos_decision_basis, "
                        "suggest_acos_suggest, suggest_acos_future_attention, suggest_budget, "
                        "suggest_budget_tag, suggest_budget_decision_basis, suggest_budget_suggest, "
                        "suggest_budget_future_attention, comprehensive_judgment, execution_pace, "
                        "risk_warning, tip_msg FROM t_advert_agent_ai_suggest "
                        "WHERE decision_id=%s LIMIT 1",
                        (decision_id,),
                    )
                    ai_suggest = cur.fetchone()
                except Exception as e:  # noqa: BLE001
                    logger.warning("read ai_suggest 降级 [%s]: %s", decision_id, e)

                direction_detail: list = []
                try:
                    cur.execute(
                        "SELECT direction_type, recommend_tag, suggest_score, content_json, "
                        "sort_order FROM t_advert_agent_direction_recommend_detail "
                        "WHERE decision_id=%s ORDER BY sort_order",
                        (decision_id,),
                    )
                    direction_detail = cur.fetchall() or []
                except Exception as e:  # noqa: BLE001
                    logger.warning("read direction_detail 降级 [%s]: %s", decision_id, e)

                direction_main = None
                try:
                    cur.execute(
                        "SELECT conclusion_json FROM t_advert_agent_direction_recommend "
                        "WHERE decision_id=%s LIMIT 1",
                        (decision_id,),
                    )
                    direction_main = cur.fetchone()
                except Exception as e:  # noqa: BLE001
                    logger.warning("read direction_recommend 降级 [%s]: %s", decision_id, e)

                core_keywords: list = []
                try:
                    cur.execute(
                        "SELECT keyword, nature_rank, nature_rank_change, keyword_type, suggest "
                        "FROM t_advert_agent_core_keyword_tracking "
                        "WHERE decision_id=%s ORDER BY create_time",
                        (decision_id,),
                    )
                    core_keywords = cur.fetchall() or []
                except Exception as e:  # noqa: BLE001
                    logger.warning("read core_keyword_tracking 降级 [%s]: %s", decision_id, e)

            return {"decision": decision, "purpose_scores": purpose_scores,
                    "ai_suggest": ai_suggest, "direction_detail": direction_detail,
                    "direction_main": direction_main, "core_keywords": core_keywords}
        finally:
            conn.close()

    def finalize_batch(
        self, decision_id: str, parent_asin: str, analysis_mode: str = "REALTIME",
    ) -> None:
        """write_full 落库后收尾：本批次置最新。

        write_full 落库后,新决策行 id=decision_id 已存在。此处把该 ASIN
        其它行 is_latest 置 0、本行置 1 + 写 analysis_mode,保证每 ASIN 唯一最新。
        进行中(run_id)标记的清除由 API 层在 state 库做,本方法不碰 state。
        decision 表无 decision_status 列——所有 decision 行即"已完成批次"。
        """
        conn = self._connect()
        now = datetime.now()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE t_advert_agent_decision SET is_latest=0 "
                    "WHERE parent_asin=%s AND is_latest=1 AND id<>%s",
                    (parent_asin, decision_id),
                )
                cur.execute(
                    "UPDATE t_advert_agent_decision "
                    "SET is_latest=1, analysis_mode=%s, update_time=%s WHERE id=%s",
                    (analysis_mode, now, decision_id),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ── 原有方法 ──────────────────────────────────────────────────────────

    def _delete_modern_campaign_children(self, cur, decision_id: str) -> None:
        """重跑同一 decision_id 时清除旧 card/pending，避免历史哈希 ID 残留。"""
        for table in (
            "t_advert_agent_modify_keyword_pending",
            "t_advert_agent_modify_campaign_pending",
            "t_advert_agent_modify_placement_pending",
            "t_advert_agent_modify_suggest_card",
        ):
            cur.execute(f"DELETE FROM {table} WHERE decision_id=%s", (decision_id,))

    def write_dual(self, run: CanonicalRun, *, include_legacy_direction: bool = True) -> WriteReport:
        report = WriteReport(decision_id=run.decision_id)
        conn = self._connect()
        now = datetime.now()
        try:
            with conn.cursor() as cur:
                if include_legacy_direction:
                    self._upsert_legacy_recommend(cur, run, now)
                    report.legacy_recommend = 1
                    report.legacy_detail = self._upsert_legacy_details(cur, run, now)
                report.legacy_metrics = self._upsert_legacy_metrics(cur, run, now)
                self._upsert_modern_summary(cur, run, now)
                report.modern_summary = 1
                self._delete_modern_campaign_children(cur, run.decision_id)
                (
                    report.modern_card,
                    report.modern_keyword_pending,
                    report.modern_campaign_pending,
                    report.modern_placement_pending,
                ) = self._upsert_modern_cards_and_pending(cur, run, now)
                self._upsert_synthesis(cur, run, now)
            conn.commit()
            return report
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def write_full(
        self,
        run: CanonicalRun,
        *,
        wizard_payload: dict[str, Any] | None = None,
        decision_meta: dict[str, Any] | None = None,
        operator: str = "tab5",
    ) -> WriteReport:
        meta = {**(run.decision_meta or {}), **(decision_meta or {})}
        # 暂存 operator，供各 _upsert_* 写 audit 列（create_by/editor_by/creator_id/editor_id）
        self._operator = operator
        self._audit_int_val = _audit_int(operator)
        report = WriteReport(decision_id=run.decision_id)
        conn = self._connect()
        now = datetime.now()
        try:
            with conn.cursor() as cur:
                self._upsert_decision(cur, run, meta, now)
                report.decision = 1
                self._upsert_decision_config(cur, run, meta, now)
                report.decision_config = 1
                report.legacy_metrics = self._upsert_legacy_metrics(cur, run, now)
                self._upsert_modern_summary(cur, run, now)
                report.modern_summary = 1
                self._delete_modern_campaign_children(cur, run.decision_id)
                (
                    report.modern_card,
                    report.modern_keyword_pending,
                    report.modern_campaign_pending,
                    report.modern_placement_pending,
                ) = self._upsert_modern_cards_and_pending(cur, run, now)
                self._upsert_synthesis(cur, run, now)
                if wizard_payload:
                    report.purpose_score = self._upsert_purpose_scores(
                        cur, run, wizard_payload, now
                    )
                    report.core_keyword_tracking = self._upsert_core_keywords(
                        cur, run, wizard_payload, now
                    )
                    report.ai_suggest = self._upsert_ai_suggest(
                        cur, run, wizard_payload, now
                    )
                    rec_id, detail_n = self._upsert_wizard_direction(
                        cur, run, wizard_payload, now
                    )
                    report.direction_recommend = 1 if rec_id else 0
                    report.direction_detail = detail_n
            conn.commit()
            return report
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _upsert_modern_summary(self, cur, run: CanonicalRun, now: datetime) -> None:
        summary_id = stable_id("sum", run.decision_id)
        s = run.summary or {}
        eliminate_count = int(s.get("to_eliminate") or 0)
        adjust_count = int(s.get("to_adjust") or 0)
        keep_count = int(s.get("to_keep") or 0)
        reactivate_count = int(s.get("to_reactivate") or 0)
        # categorized_total 纳入所有动作桶（含复评），告警只在真实漏桶时触发
        categorized_total = eliminate_count + adjust_count + keep_count + reactivate_count
        declared_total = int(run.total_campaigns or 0)

        base_warnings = list(run.raw_payload.get("warnings") or [])
        if categorized_total > 0 and declared_total != categorized_total:
            warn_msg = (
                "summary total_count mismatch: "
                f"declared={declared_total}, categorized={categorized_total}"
            )
            # 入库前一致性校验规则：不一致时告警，并将 total_count 对齐到分类口径
            logger.warning("[%s] %s", run.decision_id, warn_msg)
            base_warnings.append(warn_msg)
            total_count_to_write = categorized_total
        else:
            total_count_to_write = declared_total

        bg = run.budget_groups or {}
        op = getattr(self, "_operator", "tab5")
        aud_int = getattr(self, "_audit_int_val", None)
        sql = """
        INSERT INTO t_advert_agent_modify_suggest_summary (
            id, decision_id, shop_id, parent_asin, parent_seller_sku, site_code, batch_no,
            total_count, eliminate_count, adjust_count, keep_count, reactivate_count,
            confidence_high_count, confidence_medium_count, confidence_low_count,
            budget_impact, validation_passed, alert_count, alert_msg,
            main_push_count, main_push_budget, broad_auto_count, broad_auto_budget,
            test_new_count, test_new_budget, eliminate_bubble_count, eliminate_bubble_budget,
            main_push_current_budget, broad_auto_current_budget, test_new_current_budget,
            analysis_overview, create_count,
            create_by, editor_by, creator_id, editor_id,
            create_time, update_time
        ) VALUES (
            %s,%s,%s,%s,%s,%s,%s,
            %s,%s,%s,%s,%s,%s,%s,%s,
            %s,%s,%s,%s,
            %s,%s,%s,%s,%s,%s,%s,%s,
            %s,%s,%s,
            %s,%s,
            %s,%s,%s,%s,
            %s,%s
        )
        ON DUPLICATE KEY UPDATE
            shop_id=VALUES(shop_id),
            parent_asin=VALUES(parent_asin),
            parent_seller_sku=VALUES(parent_seller_sku),
            site_code=VALUES(site_code),
            batch_no=VALUES(batch_no),
            total_count=VALUES(total_count),
            eliminate_count=VALUES(eliminate_count),
            adjust_count=VALUES(adjust_count),
            keep_count=VALUES(keep_count),
            reactivate_count=VALUES(reactivate_count),
            confidence_high_count=VALUES(confidence_high_count),
            confidence_medium_count=VALUES(confidence_medium_count),
            confidence_low_count=VALUES(confidence_low_count),
            budget_impact=VALUES(budget_impact),
            validation_passed=VALUES(validation_passed),
            alert_count=VALUES(alert_count),
            alert_msg=VALUES(alert_msg),
            main_push_count=VALUES(main_push_count),
            main_push_budget=VALUES(main_push_budget),
            broad_auto_count=VALUES(broad_auto_count),
            broad_auto_budget=VALUES(broad_auto_budget),
            test_new_count=VALUES(test_new_count),
            test_new_budget=VALUES(test_new_budget),
            eliminate_bubble_count=VALUES(eliminate_bubble_count),
            eliminate_bubble_budget=VALUES(eliminate_bubble_budget),
            main_push_current_budget=VALUES(main_push_current_budget),
            broad_auto_current_budget=VALUES(broad_auto_current_budget),
            test_new_current_budget=VALUES(test_new_current_budget),
            analysis_overview=VALUES(analysis_overview),
            create_count=VALUES(create_count),
            editor_by=VALUES(editor_by),
            editor_id=VALUES(editor_id),
            update_time=VALUES(update_time)
        """
        cur.execute(
            sql,
            (
                summary_id,
                run.decision_id,
                run.shop_id,
                run.parent_asin,
                run.parent_seller_sku,
                run.site_code,
                run.batch_no,
                total_count_to_write,
                eliminate_count,
                adjust_count,
                keep_count,
                reactivate_count,
                s.get("confidence_high"),
                s.get("confidence_medium"),
                s.get("confidence_low"),
                s.get("estimated_budget_impact"),
                1 if run.sanity_check_passed else 0,
                len(base_warnings),
                "; ".join(base_warnings)[:500],
                bg.get("main_push_count"),
                bg.get("main_push_budget"),
                bg.get("broad_auto_count"),
                bg.get("broad_auto_budget"),
                bg.get("test_new_count"),
                bg.get("test_new_budget"),
                bg.get("eliminate_bubble_count"),
                bg.get("eliminate_bubble_budget"),
                bg.get("main_push_current_budget"),
                bg.get("broad_auto_current_budget"),
                bg.get("test_new_current_budget"),
                run.overview_text,
                run.create_count,
                op, op, str(aud_int) if aud_int is not None else None, str(aud_int) if aud_int is not None else None,
                now,
                now,
            ),
        )

    def _upsert_modern_cards_and_pending(self, cur, run: CanonicalRun, now: datetime) -> tuple[int, int, int, int]:
        card_count = 0
        keyword_count = 0
        campaign_count = 0
        placement_count = 0

        card_sql = """
        INSERT INTO t_advert_agent_modify_suggest_card (
            id, decision_id, shop_id, parent_asin, parent_seller_sku, site_code, batch_no,
            suggest_category, confidence_level, campaign_group_type, campaign_id, campaign_name,
            asin, keyword, keyword_match_type, trigger_rule, description, evidence,
            keyword_class, review_level, is_core, perf_json, is_prefiltered, prefilter_reason,
            confirm_status, execute_status, sort_order, create_time, update_time
        ) VALUES (
            %s,%s,%s,%s,%s,%s,%s,
            %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
            %s,%s,%s,%s,%s,%s,
            'PENDING','PENDING',%s,%s,%s
        )
        ON DUPLICATE KEY UPDATE
            shop_id=VALUES(shop_id),
            suggest_category=VALUES(suggest_category),
            confidence_level=VALUES(confidence_level),
            campaign_group_type=VALUES(campaign_group_type),
            campaign_id=VALUES(campaign_id),
            campaign_name=VALUES(campaign_name),
            asin=VALUES(asin),
            keyword=VALUES(keyword),
            keyword_match_type=VALUES(keyword_match_type),
            trigger_rule=VALUES(trigger_rule),
            description=VALUES(description),
            evidence=VALUES(evidence),
            keyword_class=VALUES(keyword_class),
            review_level=VALUES(review_level),
            is_core=VALUES(is_core),
            perf_json=VALUES(perf_json),
            is_prefiltered=VALUES(is_prefiltered),
            prefilter_reason=VALUES(prefilter_reason),
            sort_order=VALUES(sort_order),
            update_time=VALUES(update_time)
        """

        keyword_sql = """
        INSERT INTO t_advert_agent_modify_keyword_pending (
            id, shop_id, parent_asin, parent_seller_sku, site_code, decision_id, batch_no, suggest_card_id,
            campaign_id, campaign_name, keyword_id, keyword_text, match_type,
            old_state, new_state, old_bid, new_bid, confirm_status, execute_status, create_time, update_time
        ) VALUES (
            %s,%s,%s,%s,%s,%s,%s,%s,
            %s,%s,%s,%s,%s,%s,%s,%s,%s,'PENDING','PENDING',%s,%s
        )
        ON DUPLICATE KEY UPDATE
            shop_id=VALUES(shop_id),
            campaign_id=VALUES(campaign_id),
            keyword_id=VALUES(keyword_id),
            keyword_text=VALUES(keyword_text),
            match_type=VALUES(match_type),
            old_state=VALUES(old_state),
            new_state=VALUES(new_state),
            old_bid=VALUES(old_bid),
            new_bid=VALUES(new_bid),
            update_time=VALUES(update_time)
        """

        campaign_sql = """
        INSERT INTO t_advert_agent_modify_campaign_pending (
            id, shop_id, parent_asin, parent_seller_sku, decision_id, suggest_card_id, batch_no, site_code,
            campaign_id, campaign_name, old_state, new_state, old_budget, new_budget,
            confirm_status, execute_status, create_time, update_time
        ) VALUES (
            %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'PENDING','PENDING',%s,%s
        )
        ON DUPLICATE KEY UPDATE
            shop_id=VALUES(shop_id),
            campaign_id=VALUES(campaign_id),
            old_state=VALUES(old_state),
            new_state=VALUES(new_state),
            old_budget=VALUES(old_budget),
            new_budget=VALUES(new_budget),
            update_time=VALUES(update_time)
        """

        placement_sql = """
        INSERT INTO t_advert_agent_modify_placement_pending (
            id, shop_id, parent_asin, parent_seller_sku, decision_id, suggest_card_id, batch_no, site_code,
            campaign_id, campaign_name, placement_type, old_percent, new_percent, adjust_action, remark,
            confirm_status, execute_status, create_time, update_time
        ) VALUES (
            %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'PENDING','PENDING',%s,%s
        )
        ON DUPLICATE KEY UPDATE
            shop_id=VALUES(shop_id),
            campaign_id=VALUES(campaign_id),
            old_percent=VALUES(old_percent),
            new_percent=VALUES(new_percent),
            adjust_action=VALUES(adjust_action),
            remark=VALUES(remark),
            update_time=VALUES(update_time)
        """

        for card in run.cards:
            cur.execute(
                card_sql,
                (
                    card.card_id,
                    run.decision_id,
                    run.shop_id,
                    run.parent_asin,
                    run.parent_seller_sku,
                    run.site_code,
                    run.batch_no,
                    card.suggest_category,
                    card.confidence_level,
                    card.campaign_group_type,
                    card.campaign_id,
                    card.campaign_name,
                    card.asin,
                    card.keyword,
                    card.keyword_match_type,
                    card.trigger_rule,
                    card.description,
                    card.evidence,
                    card.keyword_class,
                    card.review_level,
                    1 if card.is_core else 0,
                    card.perf_json,
                    1 if card.is_prefiltered else 0,
                    card.prefilter_reason,
                    card.sort_order,
                    now,
                    now,
                ),
            )
            card_count += 1

            for idx, kw in enumerate(card.keyword_pending, start=1):
                kw_row_id = warehouse_pending_id(
                    "mkp", run.decision_id, card.card_id, kw.keyword_id, kw.match_type, idx,
                )
                cur.execute(
                    keyword_sql,
                    (
                        kw_row_id,
                        run.shop_id,
                        run.parent_asin,
                        run.parent_seller_sku,
                        run.site_code,
                        run.decision_id,
                        run.batch_no,
                        card.card_id,
                        card.campaign_id,
                        card.campaign_name,
                        kw.keyword_id,
                        kw.keyword_text,
                        kw.match_type,
                        kw.old_state,
                        kw.new_state,
                        kw.old_bid,
                        kw.new_bid,
                        now,
                        now,
                    ),
                )
                keyword_count += 1

            for idx, c in enumerate(card.campaign_pending, start=1):
                cp_row_id = warehouse_pending_id(
                    "mcp", run.decision_id, card.card_id, "budget", idx,
                )
                cur.execute(
                    campaign_sql,
                    (
                        cp_row_id,
                        run.shop_id,
                        run.parent_asin,
                        run.parent_seller_sku,
                        run.decision_id,
                        card.card_id,
                        run.batch_no,
                        run.site_code,
                        card.campaign_id,
                        card.campaign_name,
                        c.old_state,
                        c.new_state,
                        c.old_budget,
                        c.new_budget,
                        now,
                        now,
                    ),
                )
                campaign_count += 1

            for idx, plc in enumerate(card.placements, start=1):
                plc_row_id = warehouse_pending_id(
                    "mpl", run.decision_id, card.card_id, plc.placement_type, idx,
                )
                cur.execute(
                    placement_sql,
                    (
                        plc_row_id,
                        run.shop_id,
                        run.parent_asin,
                        run.parent_seller_sku,
                        run.decision_id,
                        card.card_id,
                        run.batch_no,
                        run.site_code,
                        card.campaign_id,
                        card.campaign_name,
                        plc.placement_type,
                        plc.old_percent,
                        plc.new_percent,
                        plc.adjust_action,
                        plc.remark,
                        now,
                        now,
                    ),
                )
                placement_count += 1

        return card_count, keyword_count, campaign_count, placement_count

    def _upsert_synthesis(self, cur, run: CanonicalRun, now: datetime) -> tuple[int, int, int]:
        """写 synthesis 三表（reason_group / member / special）。重跑先清旧。

        member.suggest_card_id 关联 card.id：用 card.campaign_key → card.id 映射，
        把 group.campaign_keys（活动名×子ASIN）翻译成 card_id。
        """
        syn = run.synthesis or {}
        groups = syn.get("groups") or []
        specials = syn.get("special_cases") or []

        # 清旧（member 无 decision_id，按 group 关联删）
        cur.execute(
            "DELETE m FROM t_advert_agent_modify_suggest_reason_group_member m "
            "JOIN t_advert_agent_modify_suggest_reason_group g ON m.group_id=g.id "
            "WHERE g.decision_id=%s",
            (run.decision_id,),
        )
        cur.execute(
            "DELETE FROM t_advert_agent_modify_suggest_reason_group WHERE decision_id=%s",
            (run.decision_id,),
        )
        cur.execute(
            "DELETE FROM t_advert_agent_modify_suggest_special WHERE decision_id=%s",
            (run.decision_id,),
        )

        # ⚠ key_to_card 按 c.campaign_key（现为关键词级）映射；card 仅存主 adjustment
        # 的 campaign_key，synthesis 中引用非主 key 的成员会映射失败被静默跳过。
        key_to_card = {c.campaign_key: c.card_id for c in run.cards if c.campaign_key}
        _cat = {"eliminate_to_low_bid_pool": "ELIMINATE", "keep": "KEEP"}

        def _s(v: Any, n: int) -> str | None:
            if v is None:
                return None
            return str(v)[:n]

        g_count = m_count = s_count = 0
        for gi, g in enumerate(groups):
            if not isinstance(g, dict):
                continue
            group_id = stable_id("rg", run.decision_id, gi)
            action = str(g.get("action") or "")
            category = _cat.get(action, "ADJUST")
            keys = g.get("campaign_keys") or []
            cur.execute(
                "INSERT INTO t_advert_agent_modify_suggest_reason_group "
                "(id, decision_id, shop_id, parent_asin, parent_seller_sku, site_code, batch_no, "
                "group_title, suggest_category, member_count, description, sort_order, create_time, update_time) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON DUPLICATE KEY UPDATE group_title=VALUES(group_title), "
                "suggest_category=VALUES(suggest_category), member_count=VALUES(member_count), "
                "description=VALUES(description), sort_order=VALUES(sort_order), update_time=VALUES(update_time)",
                (group_id, run.decision_id, run.shop_id, run.parent_asin, run.parent_seller_sku,
                 run.site_code, run.batch_no, _s(g.get("title"), 500) or "", category,
                 len(keys), g.get("narrative"), gi, now, now),
            )
            g_count += 1
            for ki, ckey in enumerate(keys):
                card_id = key_to_card.get(ckey)
                if not card_id:
                    continue
                mid = stable_id("rgm", group_id, card_id)
                cur.execute(
                    "INSERT INTO t_advert_agent_modify_suggest_reason_group_member "
                    "(id, group_id, suggest_card_id, campaign_name, sort_order, create_time, update_time) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s) "
                    "ON DUPLICATE KEY UPDATE campaign_name=VALUES(campaign_name), "
                    "sort_order=VALUES(sort_order), update_time=VALUES(update_time)",
                    (mid, group_id, card_id, _s(ckey, 500), ki, now, now),
                )
                m_count += 1
        for si, sc in enumerate(specials):
            if not isinstance(sc, dict):
                continue
            sid = stable_id("rsp", run.decision_id, si)
            cur.execute(
                "INSERT INTO t_advert_agent_modify_suggest_special "
                "(id, decision_id, shop_id, parent_asin, parent_seller_sku, site_code, batch_no, "
                "display_text, metrics_text, recommendation, sort_order, create_time, update_time) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON DUPLICATE KEY UPDATE display_text=VALUES(display_text), "
                "metrics_text=VALUES(metrics_text), recommendation=VALUES(recommendation), "
                "sort_order=VALUES(sort_order), update_time=VALUES(update_time)",
                (sid, run.decision_id, run.shop_id, run.parent_asin, run.parent_seller_sku,
                 run.site_code, run.batch_no, _s(sc.get("campaign_key"), 500) or "",
                 _s(sc.get("metrics"), 500), sc.get("why_special"), si, now, now),
            )
            s_count += 1
        return g_count, m_count, s_count

    def _upsert_legacy_recommend(self, cur, run: CanonicalRun, now: datetime) -> None:
        recommend_id = stable_id("rec", run.decision_id)
        sql = """
        INSERT INTO t_advert_agent_direction_recommend (
            id, decision_id, shop_id, parent_asin, parent_seller_sku, site_code, batch_no,
            decision_basis_json, conclusion_json, data_focus_json, create_time, update_time
        ) VALUES (
            %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s
        )
        ON DUPLICATE KEY UPDATE
            shop_id=VALUES(shop_id),
            parent_asin=VALUES(parent_asin),
            parent_seller_sku=VALUES(parent_seller_sku),
            site_code=VALUES(site_code),
            batch_no=VALUES(batch_no),
            decision_basis_json=VALUES(decision_basis_json),
            conclusion_json=VALUES(conclusion_json),
            data_focus_json=VALUES(data_focus_json),
            update_time=VALUES(update_time)
        """
        cur.execute(
            sql,
            (
                recommend_id,
                run.decision_id,
                run.shop_id,
                run.parent_asin,
                run.parent_seller_sku,
                run.site_code,
                run.batch_no,
                str(run.raw_payload.get("strategy_context") or {}),
                str(run.summary or {}),
                str(run.raw_payload.get("rounds_detail") or {}),
                now,
                now,
            ),
        )

    def _upsert_legacy_details(self, cur, run: CanonicalRun, now: datetime) -> int:
        count = 0
        sql = """
        INSERT INTO t_advert_agent_direction_recommend_detail (
            id, direction_recommend_id, decision_id, direction_type, item_count,
            recommend_tag, suggest_score, content_json, sort_order, create_time, update_time
        ) VALUES (
            %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s
        )
        ON DUPLICATE KEY UPDATE
            direction_type=VALUES(direction_type),
            item_count=VALUES(item_count),
            recommend_tag=VALUES(recommend_tag),
            suggest_score=VALUES(suggest_score),
            content_json=VALUES(content_json),
            sort_order=VALUES(sort_order),
            update_time=VALUES(update_time)
        """
        for detail in run.legacy_details:
            cur.execute(
                sql,
                (
                    detail.detail_id,
                    detail.direction_recommend_id,
                    detail.decision_id,
                    detail.direction_type,
                    len(run.legacy_details),
                    detail.recommend_tag,
                    detail.suggest_score,
                    detail.content_json,
                    detail.sort_order,
                    now,
                    now,
                ),
            )
            count += 1
        return count

    def _upsert_legacy_metrics(self, cur, run: CanonicalRun, now: datetime) -> int:
        count = 0
        sql = """
        INSERT INTO t_advert_agent_data_metrics (
            id, decision_id, metrics_type, day_str, avg_daily_sale_num, acos,
            organic_order_rate, tacos, overall_cvr, cvr, ctr, cpc, daily_cost,
            create_time, update_time
        ) VALUES (
            %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s
        )
        ON DUPLICATE KEY UPDATE
            avg_daily_sale_num=VALUES(avg_daily_sale_num),
            acos=VALUES(acos),
            organic_order_rate=VALUES(organic_order_rate),
            tacos=VALUES(tacos),
            overall_cvr=VALUES(overall_cvr),
            cvr=VALUES(cvr),
            ctr=VALUES(ctr),
            cpc=VALUES(cpc),
            daily_cost=VALUES(daily_cost),
            update_time=VALUES(update_time)
        """
        for row in run.metrics_rows:
            cur.execute(
                sql,
                (
                    row["id"],
                    row["decision_id"],
                    row["metrics_type"],
                    row["day_str"],
                    row["avg_daily_sale_num"],
                    row["acos"],
                    row["organic_order_rate"],
                    row["tacos"],
                    row["overall_cvr"],
                    row["cvr"],
                    row["ctr"],
                    row["cpc"],
                    row["daily_cost"],
                    now,
                    now,
                ),
            )
            count += 1
        return count

    def _upsert_decision(self, cur, run: CanonicalRun, meta: dict[str, Any], now: datetime) -> None:
        p3 = meta.get("p3") or {}
        target_acos = p3.get("target_acos") or {}
        budget_bid = p3.get("budget_bid") or {}
        aud = getattr(self, "_audit_int_val", None)
        # product_name 来自 listing.product_cn_name（Doris），varchar(200) 上限
        product_name = (meta.get("product_name") or "").strip()[:200] or None
        sql = """
        INSERT INTO t_advert_agent_decision (
            id, parent_asin, parent_seller_sku, shop_id, site_code, day_range,
            product_position, product_stage, season_type,
            advert_purposes, target_keyword_types,
            target_acos_suggest, daily_budget_suggest, advert_direction_types,
            batch_no, product_name,
            create_by, editor_by, creator_id, editor_id, create_time, update_time
        ) VALUES (
            %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s
        )
        ON DUPLICATE KEY UPDATE
            parent_asin=VALUES(parent_asin),
            parent_seller_sku=VALUES(parent_seller_sku),
            shop_id=VALUES(shop_id),
            site_code=VALUES(site_code),
            day_range=VALUES(day_range),
            product_position=VALUES(product_position),
            product_stage=VALUES(product_stage),
            season_type=VALUES(season_type),
            advert_purposes=VALUES(advert_purposes),
            target_keyword_types=VALUES(target_keyword_types),
            target_acos_suggest=VALUES(target_acos_suggest),
            daily_budget_suggest=VALUES(daily_budget_suggest),
            advert_direction_types=VALUES(advert_direction_types),
            batch_no=VALUES(batch_no),
            product_name=VALUES(product_name),
            editor_by=VALUES(editor_by),
            editor_id=VALUES(editor_id),
            update_time=VALUES(update_time)
        """
        cur.execute(
            sql,
            (
                run.decision_id,
                run.parent_asin,
                run.parent_seller_sku,
                run.shop_id,
                meta.get("site_code") or run.site_code,
                meta.get("day_range") or "DAY_7",
                map_product_position(meta.get("product_position")),
                map_product_stage(meta.get("product_stage")),
                map_season_type(meta.get("season_type")),
                to_enum_list(meta.get("ad_purposes"), map_purpose_target),
                to_enum_list(meta.get("target_keyword_types"), map_target_keyword_type),
                (str(_a) if (_a := target_acos.get("recommended_target")) not in (None, "") else None),
                (str(_b) if (_b := budget_bid.get("suggested")) not in (None, "") else None),
                map_direction_types_json(meta.get("advert_direction_types") or []),
                run.batch_no,
                product_name,
                aud, aud, aud, aud,
                now,
                now,
            ),
        )

    def _upsert_decision_config(self, cur, run: CanonicalRun, meta: dict[str, Any], now: datetime) -> None:
        cfg_id = stable_id("cfg", run.decision_id)
        p3 = meta.get("p3") or {}
        target_acos = p3.get("target_acos") or {}
        budget_bid = p3.get("budget_bid") or {}
        sql = """
        INSERT INTO t_advert_agent_decision_config (
            id, shop_id, parent_asin, parent_seller_sku, site_code, day_range,
            product_position, product_stage, season_type,
            advert_purposes, target_keyword_types,
            advert_direction_types,
            target_acos_suggest, daily_budget_suggest,
            create_time, update_time
        ) VALUES (
            %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s
        )
        ON DUPLICATE KEY UPDATE
            shop_id=VALUES(shop_id),
            parent_asin=VALUES(parent_asin),
            parent_seller_sku=VALUES(parent_seller_sku),
            site_code=VALUES(site_code),
            day_range=VALUES(day_range),
            product_position=VALUES(product_position),
            product_stage=VALUES(product_stage),
            season_type=VALUES(season_type),
            advert_purposes=VALUES(advert_purposes),
            target_keyword_types=VALUES(target_keyword_types),
            advert_direction_types=VALUES(advert_direction_types),
            target_acos_suggest=VALUES(target_acos_suggest),
            daily_budget_suggest=VALUES(daily_budget_suggest),
            update_time=VALUES(update_time)
        """
        # target_acos_suggest / daily_budget_suggest：记录「本次分析事件所用的」ACOS/预算
        # （resolved 值，与决策表 _upsert_decision 同源同口径）；分析完成时随 1-4 一并落库。
        cur.execute(
            sql,
            (
                cfg_id,
                run.shop_id,
                run.parent_asin,
                run.parent_seller_sku,
                meta.get("site_code") or run.site_code,
                meta.get("day_range") or "DAY_7",
                map_product_position(meta.get("product_position")),
                map_product_stage(meta.get("product_stage")),
                map_season_type(meta.get("season_type")),
                to_enum_list(meta.get("ad_purposes"), map_purpose_target),
                to_enum_list(meta.get("target_keyword_types"), map_target_keyword_type),
                map_direction_types_json(meta.get("advert_direction_types") or []),
                (str(_a) if (_a := target_acos.get("recommended_target")) not in (None, "") else None),
                (str(_b) if (_b := budget_bid.get("suggested")) not in (None, "") else None),
                now,
                now,
            ),
        )

    def _upsert_purpose_scores(
        self, cur, run: CanonicalRun, wizard: dict[str, Any], now: datetime
    ) -> int:
        scores = wizard.get("target_scores") or []
        sql = """
        INSERT INTO t_advert_agent_purpose_score (
            id, decision_id, advert_purpose, score, recommend_level,
            decision_basis, suggest, future_attention, create_time, update_time
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON DUPLICATE KEY UPDATE
            advert_purpose=VALUES(advert_purpose),
            score=VALUES(score),
            recommend_level=VALUES(recommend_level),
            decision_basis=VALUES(decision_basis),
            suggest=VALUES(suggest),
            future_attention=VALUES(future_attention),
            update_time=VALUES(update_time)
        """
        count = 0
        for idx, item in enumerate(scores, start=1):
            basis, suggest, future = split_reason_sections(item.get("reason"))
            row_id = stable_id("psc", run.decision_id, item.get("target"), idx)
            cur.execute(
                sql,
                (
                    row_id,
                    run.decision_id,
                    map_purpose_target(item.get("target")),
                    level_to_score(item.get("level")),
                    item.get("level"),
                    basis,
                    suggest,
                    future,
                    now,
                    now,
                ),
            )
            count += 1
        return count

    def _upsert_core_keywords(
        self, cur, run: CanonicalRun, wizard: dict[str, Any], now: datetime
    ) -> int:
        keywords = wizard.get("keyword_analysis") or []
        sql = """
        INSERT INTO t_advert_agent_core_keyword_tracking (
            id, decision_id, keyword, nature_rank, nature_rank_change,
            keyword_type, suggest, create_time, update_time
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON DUPLICATE KEY UPDATE
            keyword=VALUES(keyword),
            nature_rank=VALUES(nature_rank),
            nature_rank_change=VALUES(nature_rank_change),
            keyword_type=VALUES(keyword_type),
            suggest=VALUES(suggest),
            update_time=VALUES(update_time)
        """
        count = 0
        for idx, item in enumerate(keywords, start=1):
            word = item.get("word") or item.get("keyword")
            if not word:
                continue
            row_id = stable_id("ckw", run.decision_id, word, idx)
            rank_change = item.get("rank_change") or item.get("rank_change_14d")
            cur.execute(
                sql,
                (
                    row_id,
                    run.decision_id,
                    str(word)[:200],
                    str(item.get("rank") or "")[:50] or None,
                    str(rank_change or "")[:50] or None,
                    (
                        str(mapped)[:50]
                        if (mapped := map_target_keyword_type(
                            str(item.get("keyword_class") or "")
                        ))
                        else None
                    ),
                    str(item.get("action") or "")[:512] or None,
                    now,
                    now,
                ),
            )
            count += 1
        return count

    def _upsert_ai_suggest(
        self, cur, run: CanonicalRun, wizard: dict[str, Any], now: datetime
    ) -> int:
        p3 = wizard.get("p3") or {}
        if not p3:
            return 0
        row_id = stable_id("ais", run.decision_id)
        target_acos = p3.get("target_acos") or {}
        budget_bid = p3.get("budget_bid") or {}
        acos_basis, acos_suggest, acos_future = split_reason_sections(
            target_acos.get("reasoning")
        )
        budget_basis, budget_suggest, budget_future = split_reason_sections(
            budget_bid.get("reason")
        )
        overall = p3.get("overall_reasoning") or ""
        aud = getattr(self, "_audit_int_val", None)
        sql = """
        INSERT INTO t_advert_agent_ai_suggest (
            id, decision_id,
            suggest_acos, suggest_acos_tag, suggest_acos_decision_basis,
            suggest_acos_suggest, suggest_acos_future_attention,
            suggest_budget, suggest_budget_tag, suggest_budget_decision_basis,
            suggest_budget_suggest, suggest_budget_future_attention,
            suggest_keyword_adjust_list_json,
            comprehensive_judgment, execution_pace, risk_warning, tip_msg,
            create_by, editor_by, creator_id, editor_id,
            create_time, update_time
        ) VALUES (
            %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s
        )
        ON DUPLICATE KEY UPDATE
            suggest_acos=VALUES(suggest_acos),
            suggest_acos_tag=VALUES(suggest_acos_tag),
            suggest_acos_decision_basis=VALUES(suggest_acos_decision_basis),
            suggest_acos_suggest=VALUES(suggest_acos_suggest),
            suggest_acos_future_attention=VALUES(suggest_acos_future_attention),
            suggest_budget=VALUES(suggest_budget),
            suggest_budget_tag=VALUES(suggest_budget_tag),
            suggest_budget_decision_basis=VALUES(suggest_budget_decision_basis),
            suggest_budget_suggest=VALUES(suggest_budget_suggest),
            suggest_budget_future_attention=VALUES(suggest_budget_future_attention),
            suggest_keyword_adjust_list_json=VALUES(suggest_keyword_adjust_list_json),
            comprehensive_judgment=VALUES(comprehensive_judgment),
            execution_pace=VALUES(execution_pace),
            risk_warning=VALUES(risk_warning),
            tip_msg=VALUES(tip_msg),
            editor_by=VALUES(editor_by),
            editor_id=VALUES(editor_id),
            update_time=VALUES(update_time)
        """
        cur.execute(
            sql,
            (
                row_id,
                run.decision_id,
                # 空/None → NULL（数值列不接受 ''；与 _upsert_decision:1316/1317 一致）。
                # suggest_acos=int(11) 对 '' 宽容(转0,脏数据)，suggest_budget=decimal(12,2) 遇 '' 直接 1366。
                (str(_a) if (_a := target_acos.get("recommended_target")) not in (None, "") else None),
                target_acos.get("confidence"),
                acos_basis,
                acos_suggest,
                acos_future,
                (str(_b) if (_b := budget_bid.get("suggested")) not in (None, "") else None),
                budget_bid.get("direction"),
                budget_basis,
                budget_suggest,
                budget_future,
                json_dumps(budget_bid.get("bid_adjustments") or []),
                overall[:2000] if overall else None,
                None,
                "\n".join(p3.get("risk_warnings") or [])[:2000] or None,
                p3.get("message"),
                aud, aud, aud, aud,
                now,
                now,
            ),
        )
        return 1

    def _upsert_wizard_direction(
        self, cur, run: CanonicalRun, wizard: dict[str, Any], now: datetime
    ) -> tuple[str | None, int]:
        directions = wizard.get("directions") or []
        if not directions:
            return None, 0
        recommend_id = stable_id("rec", run.decision_id)
        analysis = wizard.get("analysis") or {}
        validation_report = wizard.get("validation_report") or {}
        sql_rec = """
        INSERT INTO t_advert_agent_direction_recommend (
            id, decision_id, shop_id, parent_asin, parent_seller_sku, site_code, batch_no,
            decision_basis_json, conclusion_json, data_focus_json,
            create_time, update_time
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON DUPLICATE KEY UPDATE
            shop_id=VALUES(shop_id),
            parent_asin=VALUES(parent_asin),
            parent_seller_sku=VALUES(parent_seller_sku),
            site_code=VALUES(site_code),
            batch_no=VALUES(batch_no),
            decision_basis_json=VALUES(decision_basis_json),
            conclusion_json=VALUES(conclusion_json),
            data_focus_json=VALUES(data_focus_json),
            update_time=VALUES(update_time)
        """
        display = build_whip_display_fields(
            analysis if isinstance(analysis, dict) else {},
            wizard,
        )
        cur.execute(
            sql_rec,
            (
                recommend_id,
                run.decision_id,
                run.shop_id,
                run.parent_asin,
                run.parent_seller_sku,
                run.site_code,
                run.batch_no,
                json_dumps(display.decision_basis),
                json_dumps(display.suggestion),
                json_dumps(display.future_attention),
                now,
                now,
            ),
        )
        sql_det = """
        INSERT INTO t_advert_agent_direction_recommend_detail (
            id, direction_recommend_id, decision_id, direction_type, item_count,
            recommend_tag, suggest_score, content_json, sort_order, create_time, update_time
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON DUPLICATE KEY UPDATE
            direction_type=VALUES(direction_type),
            item_count=VALUES(item_count),
            recommend_tag=VALUES(recommend_tag),
            suggest_score=VALUES(suggest_score),
            content_json=VALUES(content_json),
            sort_order=VALUES(sort_order),
            update_time=VALUES(update_time)
        """
        order_map = {
            "push_natural": 1,
            "expand_keywords": 2,
            "optimize_acos": 3,
            "balance_maintain": 4,
        }
        validations = wizard.get("validations") or {}
        decisions = wizard.get("decisions") or {}
        cur.execute(
            "DELETE FROM t_advert_agent_direction_recommend_detail WHERE decision_id=%s",
            (run.decision_id,),
        )
        count = 0
        for d in directions:
            dir_id = d.get("id") or ""
            erp_type = map_direction_type(dir_id)
            sort_order = order_map.get(dir_id, count + 1)
            detail_id = stable_id("det", run.decision_id, dir_id)
            val = validations.get(dir_id) or {}
            dec = decisions.get(dir_id) or {}
            content = build_wizard_direction_content_json(
                d,
                val,
                dec.get("decision_package"),
            )
            score = d.get("suitability_score")
            if score is not None:
                try:
                    score_int = int(float(score))
                except (TypeError, ValueError):
                    score_int = None
            else:
                score_int = None
            tag = d.get("suitability") or ("recommended" if d.get("recommended") else "available")
            cur.execute(
                sql_det,
                (
                    detail_id,
                    recommend_id,
                    run.decision_id,
                    erp_type,
                    1,
                    tag,
                    score_int,
                    json_dumps(content),
                    sort_order,
                    now,
                    now,
                ),
            )
            count += 1
        return recommend_id, count

    # ── 淘汰复评入池表（KB 21 §7 数据源） ────────────────────────────

    def get_active_entries(self, parent_asin: str) -> dict[str, dict]:
        """返回 {campaign_id: {"entry_date": datetime, "eliminate_spend_7d": float|None}}。

        返回结构与旧 get_elimination_entry_dates 完全一致
        → analyze_eliminated_restart / _run_restart_review 零改。
        仅取 exit_date IS NULL（仍在池）的记录。
        """
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT campaign_id, entry_date, eliminate_spend_7d
                       FROM t_advert_agent_pool_entry
                       WHERE parent_asin = %s AND exit_date IS NULL
                       ORDER BY entry_date DESC""",
                    (parent_asin,),
                )
                out: dict[str, dict] = {}
                for r in cur.fetchall():
                    cid = str(r.get("campaign_id") or "").strip()
                    if not cid or cid in out:
                        continue
                    out[cid] = {
                        "entry_date": r.get("entry_date"),
                        "eliminate_spend_7d": r.get("eliminate_spend_7d"),
                    }
                return out
        finally:
            conn.close()

    def sync_pool_entries(
        self,
        parent_asin: str,
        live_campaigns: "list[CampaignUnit]",
        shop_account: str | None = None,
        parent_sku: str | None = None,
        shop_id: int | None = None,
    ) -> dict[str, int]:
        """分析后双向同步池表。

        1. 读当前在池记录(exit_date IS NULL)，建集合；
        2. 以 campaign_id 为 key 从 live_campaigns 建索引；
        3. 离池方向：池表有记录但 live 无该 campaign_id,
           或 live is_strictly_in_low_bid_pool=False(已恢复) → UPDATE exit_date=NOW();
        4. 入池方向(discovery)：live is_strictly_in_low_bid_pool=True 且池表无该 campaign_id
           → INSERT source='discovery', entry_date=NOW(), exit_date=NULL;
        5. campaign_id 空串全部跳过(UNIQUE KEY 碰撞风险)。
        """
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        inserted = 0
        exited = 0

        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT campaign_id FROM t_advert_agent_pool_entry
                       WHERE parent_asin = %s AND exit_date IS NULL""",
                    (parent_asin,),
                )
                in_pool_cids: set[str] = {
                    str(r.get("campaign_id") or "").strip()
                    for r in cur.fetchall()
                }
                in_pool_cids.discard("")

                live_by_cid: dict[str, "CampaignUnit"] = {}
                for cu in live_campaigns:
                    cid = (cu.campaign_id or "").strip()
                    if not cid:
                        continue
                    live_by_cid[cid] = cu

                for cid in in_pool_cids:
                    cu = live_by_cid.get(cid)
                    if cu is None:
                        cur.execute(
                            """UPDATE t_advert_agent_pool_entry
                               SET exit_date = %s, update_time = %s
                               WHERE parent_asin = %s AND campaign_id = %s
                                 AND exit_date IS NULL""",
                            (now, now, parent_asin, cid),
                        )
                        exited += cur.rowcount
                        continue
                    if not is_strictly_in_low_bid_pool(cu.current_bid, cu.current_budget):
                        cur.execute(
                            """UPDATE t_advert_agent_pool_entry
                               SET exit_date = %s, update_time = %s
                               WHERE parent_asin = %s AND campaign_id = %s
                                 AND exit_date IS NULL""",
                            (now, now, parent_asin, cid),
                        )
                        exited += cur.rowcount

               # 4a. 批量取 pending 表执行/确认时间（discovery 入池日优先用此时间）
                discovery_cids = [
                    cid for cid, cu in live_by_cid.items()
                    if cid not in in_pool_cids
                    and is_strictly_in_low_bid_pool(cu.current_bid, cu.current_budget)
                ]
                pending_entry: dict[str, datetime] = {}
                if discovery_cids:
                    ph = ",".join(["%s"] * len(discovery_cids))
                    cur.execute(
                        f"""SELECT c.campaign_id,
                                    MIN(COALESCE(cp.execute_time, cp.confirm_time)) AS entry_time
                            FROM t_advert_agent_modify_suggest_card c
                            JOIN t_advert_agent_modify_campaign_pending cp
                              ON cp.suggest_card_id = c.id
                            WHERE c.parent_asin = %s AND c.campaign_id IN ({ph})
                              AND c.suggest_category = 'ELIMINATE'
                              AND COALESCE(cp.execute_time, cp.confirm_time) IS NOT NULL
                            GROUP BY c.campaign_id""",
                        (parent_asin, *discovery_cids),
                    )
                    for r in cur.fetchall():
                        cid = str(r.get("campaign_id") or "").strip()
                        et = r.get("entry_time")
                        if cid and et:
                            pending_entry[cid] = et

                # 4b. 入池方向(discovery)
                for cid, cu in live_by_cid.items():
                    if cid in in_pool_cids:
                        continue
                    if not is_strictly_in_low_bid_pool(cu.current_bid, cu.current_budget):
                        continue
                    entry_dt = pending_entry.get(cid, now)  # pending 执行时间 > NOW()
                    spend = _perf_json_cost(
                        cu.perf_7d.model_dump() if cu.perf_7d else None
                    )
                    cur.execute(
                        """INSERT INTO t_advert_agent_pool_entry
                           (shop_id, shop_account, parent_asin, parent_sku, child_asin, campaign_id,
                            campaign_key, campaign_name, keyword_text, decision_id, source,
                            entry_date, exit_date, eliminate_spend_7d,
                            create_time, update_time)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NULL,%s,%s,%s)
                           ON DUPLICATE KEY UPDATE
                            exit_date = NULL,
                            entry_date = VALUES(entry_date),
                            eliminate_spend_7d = VALUES(eliminate_spend_7d),
                            source = VALUES(source),
                            update_time = VALUES(update_time)""",
                        (
                            shop_id,
                            shop_account,
                            parent_asin,
                            parent_sku,
                            cu.child_asin or "",
                            cid,
                            cu.campaign_key or "",
                            cu.campaign_name or "",
                            cu.keyword_text or "",
                            None,
                            "discovery",
                            entry_dt,
                            spend,
                            now,
                            now,
                        ),
                    )
                    inserted += cur.rowcount

            conn.commit()
            if inserted or exited:
                logger.info(
                    "PoolEntry sync [%s]: discovery 入池 %d 条, 离池 %d 条",
                    parent_asin, inserted, exited,
                )
            return {"inserted": inserted, "exited": exited}
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ── 执行钩子（运营同意执行淘汰/复评后调用）──

    def upsert_pool_entry(
        self,
        parent_asin: str,
        *,
        campaign_id: str,
        child_asin: str | None = None,
        campaign_key: str | None = None,
        campaign_name: str = "",
        keyword_text: str | None = None,
        decision_id: str | None = None,
        eliminate_spend_7d: float | None = None,
        shop_id: int | None = None,
        shop_account: str | None = None,
        parent_sku: str | None = None,
    ) -> None:
        """执行淘汰→入池：source='execution', entry_date=NOW(), exit_date=NULL。

        复淘汰（同 campaign 二次淘汰）走 ON DUPLICATE KEY UPDATE：
          重置 exit_date=NULL，刷新 entry_date/spend/source 到本次执行时刻。
        """
        if not campaign_id:
            return
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO t_advert_agent_pool_entry
                       (shop_id, shop_account, parent_asin, parent_sku, child_asin, campaign_id,
                        campaign_key, campaign_name, keyword_text, decision_id, source,
                        entry_date, exit_date, eliminate_spend_7d,
                        create_time, update_time)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NULL,%s,%s,%s)
                       ON DUPLICATE KEY UPDATE
                        exit_date = NULL,
                        entry_date = VALUES(entry_date),
                        eliminate_spend_7d = VALUES(eliminate_spend_7d),
                        source = VALUES(source),
                        decision_id = VALUES(decision_id),
                        shop_id = VALUES(shop_id),
                        shop_account = VALUES(shop_account),
                        parent_sku = VALUES(parent_sku),
                        update_time = VALUES(update_time)""",
                    (
                        shop_id, shop_account, parent_asin, parent_sku,
                        child_asin or "", campaign_id,
                        campaign_key or "", campaign_name or "",
                        keyword_text or None, decision_id, "execution",
                        now, eliminate_spend_7d, now, now,
                    ),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def mark_pool_exit(self, parent_asin: str, campaign_id: str, *, exit_date=None) -> None:
        """执行复评→离池：UPDATE exit_date=NOW()（软标记，不硬删）。"""
        if not campaign_id:
            return
        ed = exit_date or datetime.now(timezone.utc).replace(tzinfo=None)
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE t_advert_agent_pool_entry
                       SET exit_date = %s, update_time = %s
                       WHERE parent_asin = %s AND campaign_id = %s
                         AND exit_date IS NULL""",
                    (ed, ed, parent_asin, campaign_id),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def get_recently_reactivated(
        self, parent_asin: str, *, max_days: int = 3,
    ) -> dict[str, int]:
        """返回 {campaign_id: days_since_reactivation}，限过去 max_days 内离池的记录。

        供 _resolve_budget_conflicts 用：复评后 N 天内禁止再次淘汰（防淘汰↔复评抖动）。
        """
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT campaign_id, exit_date
                       FROM t_advert_agent_pool_entry
                       WHERE parent_asin = %s
                         AND exit_date IS NOT NULL
                         AND exit_date >= DATE_SUB(NOW(), INTERVAL %s DAY)
                       ORDER BY exit_date DESC""",
                    (parent_asin, max_days),
                )
                now = datetime.now(timezone.utc).replace(tzinfo=None)
                out: dict[str, int] = {}
                for r in cur.fetchall():
                    cid = str(r.get("campaign_id") or "").strip()
                    if not cid or cid in out:
                        continue
                    ed = r.get("exit_date")
                    if ed is None:
                        continue
                    days = (now - ed).days
                    out[cid] = max(0, days)
                return out
        finally:
            conn.close()

    # ── 核心词判定落库 ────────────────────────────────────────

    @staticmethod
    def _latest_core_keyword_task_cursor(
        cur, parent_asin: str, parent_seller_sku: str, shop_id: int,
    ) -> dict[str, Any] | None:
        cur.execute(
            """SELECT id, finished_at
               FROM t_advert_agent_core_keyword_task
               WHERE parent_asin = %s
                 AND parent_seller_sku = %s
                 AND shop_id = %s
                 AND status = 'DONE'
               ORDER BY started_at DESC
               LIMIT 1""",
            (parent_asin, parent_seller_sku, shop_id),
        )
        return cur.fetchone()

    @staticmethod
    def _core_keyword_labels_cursor(cur, task_id: str) -> list[dict[str, Any]]:
        cur.execute(
            """SELECT keyword_text, semantic_core, semantic_evidence,
                      data_core, data_evidence, is_core
               FROM t_advert_agent_core_keyword_label
               WHERE task_id = %s
               ORDER BY id""",
            (task_id,),
        )
        return list(cur.fetchall())

    @staticmethod
    def _core_keyword_policies_cursor(
        cur, parent_asin: str, parent_seller_sku: str, shop_id: int,
    ) -> list[dict[str, Any]]:
        cur.execute(
            """SELECT keyword_text, keyword_norm, state, base_task_id,
                      base_task_finished_at, operator
               FROM t_advert_agent_core_keyword_state
               WHERE parent_asin = %s
                 AND parent_seller_sku = %s
                 AND shop_id = %s""",
            (parent_asin, parent_seller_sku, shop_id),
        )
        return list(cur.fetchall())

    def list_core_keyword_management(
        self, parent_asin: str, parent_seller_sku: str, shop_id: int,
    ) -> dict[str, Any]:
        """返回与 Campaign 当前 AI 基础同源的核心词管理单列表。"""
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                latest = self._latest_core_keyword_task_cursor(
                    cur, parent_asin, parent_seller_sku, shop_id,
                )
                if not latest:
                    return {
                        "effective_core_count": 0, "limit": 30,
                        "latest_task": None, "rows": [], "word_pool": [],
                    }
                labels = self._core_keyword_labels_cursor(cur, str(latest["id"]))
                policies = self._core_keyword_policies_cursor(
                    cur, parent_asin, parent_seller_sku, shop_id,
                )

                policy_by_norm = {
                    normalize_core_keyword(row.get("keyword_norm") or row.get("keyword_text") or ""): row
                    for row in policies
                }
                rows_by_norm: dict[str, dict[str, Any]] = {}
                for label in labels:
                    if not label.get("is_core"):
                        continue
                    norm = normalize_core_keyword(label.get("keyword_text") or "")
                    if not norm:
                        continue
                    policy = policy_by_norm.get(norm)
                    types: list[str] = []
                    if label.get("semantic_core"):
                        types.append("semantic")
                    if label.get("data_core"):
                        types.append("data")
                    if policy:
                        types.append("manual")
                    rows_by_norm[norm] = {
                        "keyword_text": label["keyword_text"], "types": types,
                        "semantic_evidence": label.get("semantic_evidence") or [],
                        "data_evidence": label.get("data_evidence") or [],
                        "manual_reason": "人工覆盖" if policy else "",
                        "state": (policy or {}).get("state") or "ENABLED",
                    }

                for norm, policy in policy_by_norm.items():
                    if norm in rows_by_norm:
                        continue
                    rows_by_norm[norm] = {
                        "keyword_text": policy.get("keyword_text") or norm,
                        "types": ["manual"], "semantic_evidence": [],
                        "data_evidence": [], "manual_reason": "人工覆盖",
                        "state": policy["state"],
                    }

                effective = resolve_effective_core_keywords(
                    [label["keyword_text"] for label in labels if label.get("is_core")],
                    {norm: row.get("state") or "ENABLED" for norm, row in policy_by_norm.items()},
                )
                return {
                    "effective_core_count": len(effective), "limit": 30,
                    "latest_task": {
                        "id": latest["id"],
                        "finished_at": core_keyword_task_version(latest.get("finished_at")),
                    },
                    "rows": sorted(rows_by_norm.values(), key=lambda row: row["keyword_text"].casefold()),
                    "word_pool": list(dict.fromkeys(
                        label["keyword_text"] for label in labels if label.get("keyword_text")
                    )),
                }
        finally:
            conn.close()

    def upsert_core_keyword_policy(
        self, parent_asin: str, parent_seller_sku: str, shop_id: int, keyword_text: str,
        state: str, expected_task_id: str, expected_task_finished_at: str | None,
        operator: str | None = None,
    ) -> dict[str, Any]:
        state = state.upper().strip()
        if state not in CORE_KEYWORD_POLICY_STATES:
            raise ValueError("无效核心词状态")
        keyword_text = keyword_text.strip()
        keyword_norm = normalize_core_keyword(keyword_text)
        if not keyword_norm:
            raise ValueError("核心词不能为空")
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                latest = self._latest_core_keyword_task_cursor(cur, parent_asin, parent_seller_sku, shop_id)
                if not latest or str(latest["id"]) != expected_task_id:
                    raise RuntimeError("STALE_TASK")
                if expected_task_finished_at and core_keyword_task_version(
                    latest.get("finished_at"),
                ) != core_keyword_task_version(expected_task_finished_at):
                    raise RuntimeError("STALE_TASK")
                labels = self._core_keyword_labels_cursor(cur, str(latest["id"]))
                policies = self._core_keyword_policies_cursor(cur, parent_asin, parent_seller_sku, shop_id)
                existing = {normalize_core_keyword(row.get("keyword_norm") or row.get("keyword_text") or ""): row for row in policies}
                word_pool = {normalize_core_keyword(row.get("keyword_text") or "") for row in labels}
                if keyword_norm not in word_pool and keyword_norm not in existing:
                    raise ValueError("核心词不在当前离线任务词池")
                current = existing.get(keyword_norm)
                current_state = (current or {}).get("state") or "ENABLED"
                if not current or current_state != state:
                    next_states = {norm: row.get("state") or "ENABLED" for norm, row in existing.items()}
                    next_states[keyword_norm] = state
                    effective = resolve_effective_core_keywords(
                        [row["keyword_text"] for row in labels if row.get("is_core")], next_states,
                    )
                    if len(effective) > 30:
                        raise ValueError("有效核心词超过 30 条上限")
                    cur.execute(
                        """INSERT INTO t_advert_agent_core_keyword_state
                           (parent_asin,parent_seller_sku,shop_id,keyword_text,keyword_norm,state,
                            base_task_id,base_task_finished_at,operator)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                           ON DUPLICATE KEY UPDATE keyword_text=VALUES(keyword_text), state=VALUES(state),
                            base_task_id=VALUES(base_task_id), base_task_finished_at=VALUES(base_task_finished_at),
                            operator=VALUES(operator)""",
                        (parent_asin,parent_seller_sku,shop_id,keyword_text,keyword_norm,state,
                         latest["id"],latest.get("finished_at"),operator),
                    )
            conn.commit()
        finally:
            conn.close()
        return self.list_core_keyword_management(parent_asin, parent_seller_sku, shop_id)

    def _sync_core_keyword_policies_after_task(self, cur, analysis: dict, now: datetime) -> None:
        """离线任务成功后轮换默认状态，长期人工锁定/否决不被覆盖。"""
        identity = (
            analysis["parent_asin"], analysis["parent_seller_sku"], analysis["shop_id"],
        )
        cur.execute(
            """DELETE FROM t_advert_agent_core_keyword_state
               WHERE parent_asin = %s AND parent_seller_sku = %s AND shop_id = %s
                 AND state IN ('ENABLED', 'DISABLED')""",
            identity,
        )
        for label in analysis.get("labels") or []:
            if not label.get("is_core"):
                continue
            keyword_text = str(label.get("keyword_text") or "").strip()
            keyword_norm = normalize_core_keyword(keyword_text)
            if not keyword_norm:
                continue
            cur.execute(
                """INSERT INTO t_advert_agent_core_keyword_state
                   (parent_asin, parent_seller_sku, shop_id, keyword_text, keyword_norm,
                    state, base_task_id, base_task_finished_at, operator)
                   VALUES (%s,%s,%s,%s,%s,'ENABLED',%s,%s,'offline-sync')
                   ON DUPLICATE KEY UPDATE
                    keyword_text=VALUES(keyword_text),
                    base_task_id=VALUES(base_task_id),
                    base_task_finished_at=VALUES(base_task_finished_at),
                    state=IF(state IN ('LOCKED', 'VETOED'), state, 'ENABLED')""",
                (*identity, keyword_text, keyword_norm, analysis["task_id"], now),
            )

    def write_core_keyword_task(self, analysis: dict) -> None:
        conn = self._connect()
        try:
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            labels = analysis.get("labels") or []
            source = analysis.get("source_refs")
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO t_advert_agent_core_keyword_task
                       (id, parent_asin, parent_seller_sku, shop_id, site_code,
                        status, total_keyword_count, core_keyword_count,
                        triggered_by, started_at, finished_at)
                       VALUES (%s,%s,%s,%s,%s,'DONE',%s,%s,%s,%s,%s)
                       ON DUPLICATE KEY UPDATE
                        status='DONE', total_keyword_count=VALUES(total_keyword_count),
                        core_keyword_count=VALUES(core_keyword_count),
                        finished_at=VALUES(finished_at)""",
                    (analysis["task_id"], analysis["parent_asin"],
                     analysis["parent_seller_sku"], analysis["shop_id"],
                     analysis.get("site_code") or "", len(labels),
                     analysis["core_keyword_count"],
                     analysis.get("triggered_by") or "", now, now),
                )
                # 同一 task 的落库重试必须保持幂等：label 是本次全量快照，
                # 不能让一次失败重试前的旧词残留在该 task 中。
                cur.execute(
                    "DELETE FROM t_advert_agent_core_keyword_label WHERE task_id = %s",
                    (analysis["task_id"],),
                )
                for row in labels:
                    cur.execute(
                        """INSERT INTO t_advert_agent_core_keyword_label
                           (task_id, parent_asin, parent_seller_sku, shop_id,
                            keyword_text, semantic_conflict, conflict_reason,
                            semantic_core, semantic_evidence, data_core, data_evidence,
                            is_core, source_refs)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                           ON DUPLICATE KEY UPDATE
                            semantic_conflict=VALUES(semantic_conflict),
                            conflict_reason=VALUES(conflict_reason),
                            semantic_core=VALUES(semantic_core),
                            semantic_evidence=VALUES(semantic_evidence),
                            data_core=VALUES(data_core),
                            data_evidence=VALUES(data_evidence),
                            is_core=VALUES(is_core),
                            source_refs=VALUES(source_refs)""",
                        (analysis["task_id"], analysis["parent_asin"],
                         analysis["parent_seller_sku"], analysis["shop_id"],
                         row["keyword_text"], row["semantic_conflict"],
                         row.get("conflict_reason") or None,
                         row["semantic_core"], row.get("semantic_evidence"),
                         row["data_core"], row.get("data_evidence"),
                         row["is_core"], source),
                    )
                self._sync_core_keyword_policies_after_task(cur, analysis, now)
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def fetch_core_keyword_exclusion_set(
        parent_asin: str, parent_seller_sku: str, shop_id: int,
    ) -> set[str]:
        """读取会跳过核心词离线资源调用的人工锁定/否决词。"""
        repo = _get_repository()
        conn = repo._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT keyword_norm
                       FROM t_advert_agent_core_keyword_state
                       WHERE parent_asin = %s
                         AND parent_seller_sku = %s
                         AND shop_id = %s
                         AND state IN ('LOCKED', 'VETOED')""",
                    (parent_asin, parent_seller_sku, shop_id),
                )
                return {
                    normalize_core_keyword(row.get("keyword_norm") or "")
                    for row in cur.fetchall()
                    if normalize_core_keyword(row.get("keyword_norm") or "")
                }
        except Exception:
            logger.warning(
                "fetch_core_keyword_exclusion_set 失败 [%s/%s/%s]，按无排除继续",
                parent_asin, parent_seller_sku, shop_id, exc_info=True,
            )
            return set()
        finally:
            if conn:
                conn.close()

    @staticmethod
    def fetch_core_keyword_set(
        parent_asin: str, parent_seller_sku: str, shop_id: int,
    ) -> set[str]:
        if not settings.core_keyword_enabled:
            return set()
        repo = _get_repository()
        conn = repo._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT l.keyword_text
                       FROM t_advert_agent_core_keyword_label l
                       JOIN t_advert_agent_core_keyword_task t ON l.task_id = t.id
                       WHERE l.parent_asin = %s
                         AND l.parent_seller_sku = %s
                         AND l.shop_id = %s
                         AND l.is_core = 1
                         AND t.status = 'DONE'
                         AND t.id = (
                             SELECT id
                             FROM t_advert_agent_core_keyword_task
                             WHERE parent_asin = %s
                               AND parent_seller_sku = %s
                               AND shop_id = %s
                               AND status = 'DONE'
                             ORDER BY started_at DESC, finished_at DESC, id DESC
                             LIMIT 1
                         )""",
                    (parent_asin, parent_seller_sku, shop_id,
                     parent_asin, parent_seller_sku, shop_id),
                )
                ai_keywords = {r["keyword_text"] for r in cur.fetchall()}
                policies = ErpDualWriterRepository._core_keyword_policies_cursor(
                    cur, parent_asin, parent_seller_sku, shop_id,
                )
                return resolve_effective_core_keywords(
                    ai_keywords,
                    {
                        normalize_core_keyword(row.get("keyword_norm") or row.get("keyword_text") or ""): row.get("state") or "ENABLED"
                        for row in policies
                    },
                )
        except Exception:
            logger.warning(
                "fetch_core_keyword_set 失败 [%s/%s/%s]，按空集继续",
                parent_asin, parent_seller_sku, shop_id, exc_info=True,
            )
            return set()
        finally:
            if conn:
                conn.close()


# ── 模块级 repository 单例（决策批次端点复用） ──────────────────────────

_repo_instance: ErpDualWriterRepository | None = None


def _perf_json_cost(perf_json) -> float | None:
    """解 card.perf_json 取 cost（淘汰前7d花费）；空/非法 → None。"""
    if not perf_json:
        return None
    try:
        d = perf_json if isinstance(perf_json, dict) else json.loads(perf_json)
        v = d.get("cost")
        return float(v) if v is not None else None
    except (ValueError, TypeError, AttributeError):
        return None


def _get_repository() -> ErpDualWriterRepository:
    """延迟创建 ERP 仓储单例，避免启动时 DB 不通炸模块加载。"""
    global _repo_instance
    if _repo_instance is None:
        from app.config.settings import settings
        _repo_instance = ErpDualWriterRepository(
            host=settings.erp_host,
            port=settings.erp_port,
            user=settings.erp_user,
            password=settings.erp_password,
            database=settings.erp_database,
            use_tls=settings.erp_use_tls,
        )
    return _repo_instance


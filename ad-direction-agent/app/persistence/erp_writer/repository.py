from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import logging
from typing import Any

import pymysql
from pymysql.cursors import DictCursor

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

logger = logging.getLogger(__name__)


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
    def __init__(self, *, host: str, port: int, user: str, password: str, database: str):
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

    def _connect(self):
        return pymysql.connect(**self._conn_kwargs)

    # ── 批次生命周期（决策批次改造方案 §一） ──────────────────────────────

    def list_batches(self, asin: str) -> list[dict]:
        """返回该 ASIN 所有决策批次，按 create_time 降序。"""
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT id AS decision_id, decision_status,
                              analysis_mode, is_latest, batch_no,
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
        """返回 is_latest=1 的 COMPLETED 批次。"""
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT id AS decision_id, decision_status,
                              analysis_mode, is_latest, batch_no,
                              create_time AS completed_at
                       FROM t_advert_agent_decision
                       WHERE parent_asin = %s AND is_latest = 1
                       LIMIT 1""",
                    (asin,),
                )
                return cur.fetchone()
        finally:
            conn.close()

    def get_draft(self, asin: str) -> dict | None:
        """返回该 ASIN 当前 DRAFT（仅一个）。"""
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT id AS decision_id, decision_status,
                              analysis_mode, is_latest, batch_no
                       FROM t_advert_agent_decision
                       WHERE parent_asin = %s AND decision_status = 'DRAFT'
                       LIMIT 1""",
                    (asin,),
                )
                return cur.fetchone()
        finally:
            conn.close()

    def create_draft(
        self, asin: str, decision_id: str, batch_no: str, analysis_mode: str,
    ) -> None:
        """插入 DRAFT 批次。parent_asin 填 ASIN 原值。"""
        now = datetime.now(timezone.utc)
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO t_advert_agent_decision
                       (id, parent_asin, decision_status, is_latest,
                        analysis_mode, batch_no, create_time, update_time)
                       VALUES (%s,%s,'DRAFT',0,%s,%s,%s,%s)""",
                    (decision_id, asin, analysis_mode, batch_no, now, now),
                )
            conn.commit()
        finally:
            conn.close()

    def delete_draft(self, decision_id: str) -> None:
        """删除 DRAFT 批次（仅 DRAFT 可删，COMPLETED 受保护）。"""
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """DELETE FROM t_advert_agent_decision
                       WHERE id = %s AND decision_status = 'DRAFT'""",
                    (decision_id,),
                )
            conn.commit()
        finally:
            conn.close()

    def mark_completed(self, decision_id: str) -> None:
        """DRAFT → COMPLETED + 翻 is_latest（事务:旧置0→新置1）。

        仅对 decision_status='DRAFT' 的行生效，COMPLETED 翻转为 no-op。
        """
        conn = self._connect()
        try:
            # 先查出该 DRAFT 的 parent_asin
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT parent_asin FROM t_advert_agent_decision WHERE id=%s AND decision_status='DRAFT'",
                    (decision_id,),
                )
                row = cur.fetchone()
                if not row:
                    return  # 不是 DRAFT，不操作
                asin = row["parent_asin"]

            # 事务:旧 COMPLETED is_latest→0，新 COMPLETED is_latest→1
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE t_advert_agent_decision SET is_latest=0 WHERE parent_asin=%s AND is_latest=1",
                    (asin,),
                )
                cur.execute(
                    """UPDATE t_advert_agent_decision
                       SET decision_status='COMPLETED', is_latest=1, update_time=%s
                       WHERE id=%s""",
                    (datetime.now(timezone.utc), decision_id),
                )
            conn.commit()
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
            return {
                "decision": decision,
                "summary": summary,
                "cards": cards,
                "campaign_pending": campaign_pending,
                "keyword_pending": keyword_pending,
                "placement_pending": placement_pending,
            }
        finally:
            conn.close()

    def confirm_decisions(
        self, decision_id: str, decisions: list[dict], operator: str | None = None,
    ) -> dict:
        """运营审核写回 card + 子 pending 的 confirm_status（PENDING→CONFIRMED/REJECTED）。

        - 校验可执行：批次 decision_status='COMPLETED' 且 is_latest=1 且该 ASIN 无 DRAFT。
        - 每活动只处理一次：UPDATE 限 confirm_status='PENDING'，重复处理 rowcount=0 → skipped。
        decisions: [{campaign_key: <card_id>, decision: 'approve'|'reject'}]
        """
        conn = self._connect()
        now = datetime.now()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT parent_asin, is_latest, decision_status "
                    "FROM t_advert_agent_decision WHERE id=%s",
                    (decision_id,),
                )
                d = cur.fetchone()
                if not d:
                    return {"ok": False, "error": "decision 不存在", "applied": 0, "skipped": 0}
                if d.get("decision_status") != "COMPLETED" or not d.get("is_latest"):
                    return {"ok": False, "error": "该批次非最新已完成批次，不可执行",
                            "applied": 0, "skipped": 0}
                cur.execute(
                    "SELECT 1 FROM t_advert_agent_decision "
                    "WHERE parent_asin=%s AND decision_status='DRAFT' LIMIT 1",
                    (d["parent_asin"],),
                )
                if cur.fetchone():
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
                        "SET confirm_status=%s, update_time=%s "
                        "WHERE id=%s AND decision_id=%s AND confirm_status='PENDING'",
                        (status, now, card_id, decision_id),
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
                            f"UPDATE {table} SET confirm_status=%s, update_time=%s "
                            "WHERE suggest_card_id=%s AND confirm_status='PENDING'",
                            (status, now, card_id),
                        )
            conn.commit()
            return {"ok": True, "applied": applied, "skipped": skipped}
        except Exception:
            conn.rollback()
            raise
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
                    "advert_direction_types, analysis_mode, decision_status, is_latest, "
                    "batch_no, create_time, update_time "
                    "FROM t_advert_agent_decision WHERE id=%s",
                    (decision_id,),
                )
                return cur.fetchone()
        finally:
            conn.close()

    def finalize_batch(
        self, decision_id: str, parent_asin: str, analysis_mode: str = "REALTIME",
    ) -> None:
        """write_full 落库后收尾：本批次置最新 COMPLETED + 清该 ASIN 的 DRAFT 锁。

        DRAFT-as-lock 模型：new-event 建 DRAFT 锁(uuid)，执行层 write_full 另建 COMPLETED 行
        (stable_id)。此处删 DRAFT 锁 + 翻 is_latest 到 COMPLETED 行，二者 decision_id 无需一致。
        write_full 新行 decision_status 默认 'COMPLETED'、is_latest=0、analysis_mode 默认 'REALTIME'。
        定时跑无 DRAFT 锁 → DELETE 为 no-op。
        """
        conn = self._connect()
        now = datetime.now()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM t_advert_agent_decision "
                    "WHERE parent_asin=%s AND decision_status='DRAFT'",
                    (parent_asin,),
                )
                cur.execute(
                    "UPDATE t_advert_agent_decision SET is_latest=0 "
                    "WHERE parent_asin=%s AND is_latest=1 AND id<>%s",
                    (parent_asin, decision_id),
                )
                cur.execute(
                    "UPDATE t_advert_agent_decision "
                    "SET is_latest=1, decision_status='COMPLETED', analysis_mode=%s, update_time=%s "
                    "WHERE id=%s",
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
    ) -> WriteReport:
        meta = {**(run.decision_meta or {}), **(decision_meta or {})}
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
        categorized_total = eliminate_count + adjust_count + keep_count
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
        sql = """
        INSERT INTO t_advert_agent_modify_suggest_summary (
            id, decision_id, shop_id, parent_asin, parent_seller_sku, site_code, batch_no,
            total_count, eliminate_count, adjust_count, keep_count,
            confidence_high_count, confidence_medium_count, confidence_low_count,
            budget_impact, validation_passed, alert_count, alert_msg,
            main_push_count, main_push_budget, broad_auto_count, broad_auto_budget,
            test_new_count, test_new_budget, eliminate_bubble_count, eliminate_bubble_budget,
            create_time, update_time
        ) VALUES (
            %s,%s,%s,%s,%s,%s,%s,
            %s,%s,%s,%s,%s,%s,%s,
            %s,%s,%s,%s,
            %s,%s,%s,%s,%s,%s,%s,%s,
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
                s.get("confidence_high"),
                s.get("confidence_medium"),
                s.get("confidence_low"),
                s.get("estimated_budget_impact"),
                1 if run.sanity_check_passed else 0,
                len(base_warnings),
                "; ".join(base_warnings),
                bg.get("main_push_count"),
                bg.get("main_push_budget"),
                bg.get("broad_auto_count"),
                bg.get("broad_auto_budget"),
                bg.get("test_new_count"),
                bg.get("test_new_budget"),
                bg.get("eliminate_bubble_count"),
                bg.get("eliminate_bubble_budget"),
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
            confirm_status, execute_status, sort_order, create_time, update_time
        ) VALUES (
            %s,%s,%s,%s,%s,%s,%s,
            %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
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
                    card.sort_order,
                    now,
                    now,
                ),
            )
            card_count += 1

            for idx, kw in enumerate(card.keyword_pending, start=1):
                kw_row_id = warehouse_pending_id(
                    "mkp", run.decision_id, card.campaign_id, kw.keyword_id, kw.match_type, idx,
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
                    "mcp", run.decision_id, card.campaign_id, "budget", idx,
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
                    "mpl", run.decision_id, card.campaign_id, plc.placement_type, idx,
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
        sql = """
        INSERT INTO t_advert_agent_decision (
            id, parent_asin, parent_seller_sku, shop_id, site_code, day_range,
            product_position, product_stage, season_type,
            advert_purposes, target_keyword_types,
            target_acos_suggest, daily_budget_suggest, advert_direction_types,
            batch_no, create_time, update_time
        ) VALUES (
            %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s
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
                str(target_acos.get("recommended_target") or ""),
                str(budget_bid.get("suggested") or ""),
                map_direction_types_json(meta.get("advert_direction_types") or []),
                run.batch_no,
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
            target_acos_suggest, daily_budget_suggest, advert_direction_types,
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
            target_acos_suggest=VALUES(target_acos_suggest),
            daily_budget_suggest=VALUES(daily_budget_suggest),
            advert_direction_types=VALUES(advert_direction_types),
            update_time=VALUES(update_time)
        """
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
                str(target_acos.get("recommended_target") or ""),
                str(budget_bid.get("suggested") or ""),
                map_direction_types_json(meta.get("advert_direction_types") or []),
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
        sql = """
        INSERT INTO t_advert_agent_ai_suggest (
            id, decision_id,
            suggest_acos, suggest_acos_tag, suggest_acos_decision_basis,
            suggest_acos_suggest, suggest_acos_future_attention,
            suggest_budget, suggest_budget_tag, suggest_budget_decision_basis,
            suggest_budget_suggest, suggest_budget_future_attention,
            suggest_keyword_adjust_list_json,
            comprehensive_judgment, execution_pace, risk_warning, tip_msg,
            create_time, update_time
        ) VALUES (
            %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s
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
            update_time=VALUES(update_time)
        """
        cur.execute(
            sql,
            (
                row_id,
                run.decision_id,
                str(target_acos.get("recommended_target") or "")[:50],
                target_acos.get("confidence"),
                acos_basis,
                acos_suggest,
                acos_future,
                str(budget_bid.get("suggested") or "")[:50],
                budget_bid.get("direction"),
                budget_basis,
                budget_suggest,
                budget_future,
                json_dumps(budget_bid.get("bid_adjustments") or []),
                overall[:2000] if overall else None,
                None,
                "\n".join(p3.get("risk_warnings") or [])[:2000] or None,
                p3.get("message"),
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


# ── 模块级 repository 单例（决策批次端点复用） ──────────────────────────

_repo_instance: ErpDualWriterRepository | None = None


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
        )
    return _repo_instance


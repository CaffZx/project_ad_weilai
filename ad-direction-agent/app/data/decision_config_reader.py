"""从 prod ERP t_advert_agent_decision_config 读回运营手动确认的 1-4 配置。

用途：批量定时分析（cfg_source=config）统一从 config 读 1-4，实现读写同源。
- 读出后经反向 mapper 转成 build_campaign_strategy_context / LLM prompt 吃的内部(中文)格式。
- 复用 erp_writer repository 的连接（同一 prod ERP）。
- 不返回 ACOS/预算（config 的这两列含历史 AI 值，不可信；分析层另走 override+AI 现算）。
"""

from __future__ import annotations

import logging

from app.persistence.erp_writer.repository import _get_repository
from app.persistence.erp_writer.text_utils import (
    from_enum_list,
    normalize_advert_direction_types_list,
    unmap_ad_purpose,
    unmap_product_position,
    unmap_product_stage,
    unmap_operating_mode,
    unmap_season_type,
    unmap_target_keyword_type,
)

logger = logging.getLogger(__name__)


def agent_config_row_to_layer14(row: dict) -> dict:
    """仅转换新表承载的战略层三项；其余层仍以旧表为准。"""
    return {
        "product_level": unmap_product_position(row.get("product_position")) or "",
        "operating_mode": unmap_operating_mode(row.get("operating_mode")) or "",
        "season_stage": unmap_season_type(row.get("season_type")) or "",
    }


def load_batch_layer14(asin: str, parent_seller_sku: str | None, shop_id: int | None) -> dict | None:
    """定时批跑：战略层三项优先新表，其余配置固定读旧表。"""
    asin = (asin or "").strip()
    sku = (parent_seller_sku or "").strip()
    try:
        sid = int(shop_id)
    except (TypeError, ValueError):
        sid = 0
    legacy = load_layer14(asin, sku or None)
    if not legacy:
        return None
    legacy["config_source"] = "decision_config"
    if asin and sku and sid > 0:
        conn = None
        try:
            conn = _get_repository()._connect()
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT product_position, operating_mode, season_type, advert_direction_types "
                    "FROM t_advet_agent_config "
                    "WHERE parent_asin=%s AND parent_seller_sku=%s AND shop_id=%s LIMIT 1",
                    (asin, sku, sid),
                )
                row = cur.fetchone()
            if row:
                legacy["long_term"].update(agent_config_row_to_layer14(row))
                ad_from_agent = normalize_advert_direction_types_list(row.get("advert_direction_types"))
                if ad_from_agent:
                    legacy["ad_directions"] = ad_from_agent
                    legacy["config_source"] = "agent_config"
                else:
                    legacy["config_source"] = "agent_config_strategy"
                return legacy
        except Exception as e:  # noqa: BLE001
            logger.warning("agent_config 批跑读取失败 [%s]: %s", asin, e)
        finally:
            if conn:
                conn.close()
    return legacy


def load_layer14(asin: str, parent_seller_sku: str | None = None) -> dict | None:
    """读 decision_config 的 1-4 配置，反向映射成内部(中文)格式。

    返回 {long_term:{product_level,product_stage,season_stage,ad_purposes,
          target_keyword_strategy}, ad_directions:[ERP码...], day_range} 或 None。
    ad_directions 保留 ERP 码（PUSH_NATURAL…），下游 _zh_ad_direction / map_direction_type 处理。
    """
    asin = (asin or "").strip()
    if not asin:
        return None
    try:
        conn = _get_repository()._connect()
    except Exception as e:  # noqa: BLE001
        logger.warning("decision_config 连接失败 [%s]: %s", asin, e)
        return None
    try:
        with conn.cursor() as cur:
            if parent_seller_sku:
                cur.execute(
                    "SELECT product_position, product_stage, season_type, operating_mode, "
                    "advert_purposes, target_keyword_types, advert_direction_types, day_range "
                    "FROM t_advert_agent_decision_config "
                    "WHERE parent_asin=%s AND parent_seller_sku=%s "
                    "ORDER BY update_time DESC LIMIT 1",
                    (asin, parent_seller_sku),
                )
            else:
                cur.execute(
                    "SELECT product_position, product_stage, season_type, operating_mode, "
                    "advert_purposes, target_keyword_types, advert_direction_types, day_range "
                    "FROM t_advert_agent_decision_config "
                    "WHERE parent_asin=%s ORDER BY update_time DESC LIMIT 1",
                    (asin,),
                )
            row = cur.fetchone()
        if not row:
            return None
        long_term = {
            "product_level": unmap_product_position(row.get("product_position")) or "",
            "product_stage": unmap_product_stage(row.get("product_stage")) or "",
            "season_stage": unmap_season_type(row.get("season_type")) or "",
            "operating_mode": unmap_operating_mode(row.get("operating_mode")),
            "ad_purposes": from_enum_list(row.get("advert_purposes"), unmap_ad_purpose),
            "target_keyword_strategy": from_enum_list(
                row.get("target_keyword_types"), unmap_target_keyword_type),
        }
        ad_directions = normalize_advert_direction_types_list(row.get("advert_direction_types"))
        return {
            "long_term": long_term,
            "ad_directions": ad_directions,
            "day_range": row.get("day_range"),
        }
    except Exception as e:  # noqa: BLE001
        logger.warning("load_layer14(%s) 失败: %s", asin, e)
        return None
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass

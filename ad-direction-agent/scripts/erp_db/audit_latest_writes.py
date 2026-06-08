"""Audit latest B0B7 / B0CGH9 ERP writes — enum codes + data quality."""
from __future__ import annotations

import json
import sys
from decimal import Decimal
from pathlib import Path

import pymysql

from _bootstrap import REPO_ROOT
from _erp_conn import erp_pymysql_conn

DECISIONS = [
    ("B0B7S3PWWB", "dec555e98e7cf27ac40ba2f2cd3209b9", REPO_ROOT / "cursor临时文件" / "B0B7S3PWWB_kb.jsonl", 105),
    ("B0CGH9QRKK", "dec5da0a62fe024fa952815cd44be083", REPO_ROOT / "cursor临时文件" / "B0CGH9QRKK_kb.jsonl", 56),
]

VALID_SITE = {"Amazon_US", "Amazon_DE", "Amazon_UK"}
VALID_POSITION = {"P0_PRODUCT", "P1_PRODUCT", "P2_PRODUCT", "P3_PRODUCT"}
VALID_STAGE = {"HARVEST_PROFIT", "TESTING", "PROMOTING", "MAINTAINING"}
VALID_SEASON = {"OFF_SEASON", "PEAK_SEASON_PREPARE", "BIG_PEAK_SEASON", "LATE_PEAK_SEASON"}
VALID_PURPOSE = {"TRAFFIC", "CONVERSION", "RANKING", "PROFIT"}
VALID_KEYWORD_TYPE = {"GENERIC", "LONG_TAIL", "COMPETITOR", "BRAND", "CUSTOM"}
VALID_DIRECTION = {"PUSH_NATURAL", "EXPAND_KEYWORDS", "OPTIMIZE_ACOS", "BALANCE_MAINTAIN"}


def ser(o):
    if isinstance(o, Decimal):
        return float(o)
    if isinstance(o, dict):
        return {k: ser(v) for k, v in o.items()}
    return o


def _parse_json_list(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        val = json.loads(raw)
        return [str(x) for x in val] if isinstance(val, list) else []
    except json.JSONDecodeError:
        return []


def _check_enum(issues: list[str], label: str, value: str | None, allowed: set[str]) -> None:
    if value is None:
        issues.append(f"{label}: NULL")
    elif value not in allowed:
        issues.append(f"{label}: invalid '{value}' (expected one of {sorted(allowed)})")


def _check_json_enum_list(issues: list[str], label: str, raw: str | None, allowed: set[str]) -> None:
    items = _parse_json_list(raw)
    if not items:
        issues.append(f"{label}: empty or invalid JSON")
        return
    for item in items:
        if item not in allowed:
            issues.append(f"{label}: invalid element '{item}'")


def main() -> int:
    conn = pymysql.connect(**erp_pymysql_conn())
    cur = conn.cursor()
    all_issues: list[str] = []

    for asin, did, kb_path, expected_cards in DECISIONS:
        kb = json.loads(kb_path.read_text(encoding="utf-8").splitlines()[0])
        adj = kb.get("adjustments") or []
        uniq_campaign = len(
            {a.get("campaign_id") for a in adj if a.get("campaign_id")}
        )
        issues: list[str] = []
        print(f"=== {asin} {did} ===")

        cur.execute(
            """
            SELECT shop_id, site_code, product_position, product_stage, season_type, day_range,
                   advert_purposes, target_keyword_types, advert_direction_types
            FROM t_advert_agent_decision WHERE id=%s
            """,
            (did,),
        )
        dec = cur.fetchone()
        print("decision:", ser(dec))

        _check_enum(issues, "site_code", dec.get("site_code"), VALID_SITE)
        _check_enum(issues, "product_position", dec.get("product_position"), VALID_POSITION)
        _check_enum(issues, "product_stage", dec.get("product_stage"), VALID_STAGE)
        _check_enum(issues, "season_type", dec.get("season_type"), VALID_SEASON)
        _check_json_enum_list(issues, "advert_purposes", dec.get("advert_purposes"), VALID_PURPOSE)
        _check_json_enum_list(
            issues, "target_keyword_types", dec.get("target_keyword_types"), VALID_KEYWORD_TYPE
        )
        _check_json_enum_list(
            issues, "advert_direction_types", dec.get("advert_direction_types"), VALID_DIRECTION
        )

        cur.execute(
            "SELECT direction_type FROM t_advert_agent_direction_recommend_detail WHERE decision_id=%s",
            (did,),
        )
        for r in cur.fetchall():
            dt = r.get("direction_type")
            if dt not in VALID_DIRECTION:
                issues.append(f"direction_recommend_detail.direction_type: invalid '{dt}'")

        cur.execute(
            "SELECT site_code FROM t_advert_agent_modify_suggest_summary WHERE decision_id=%s",
            (did,),
        )
        summ_site = cur.fetchone()
        if summ_site and summ_site.get("site_code") not in VALID_SITE:
            issues.append(f"summary.site_code: invalid '{summ_site.get('site_code')}'")

        cur.execute(
            "SELECT DISTINCT site_code FROM t_advert_agent_modify_suggest_card WHERE decision_id=%s",
            (did,),
        )
        for r in cur.fetchall():
            if r.get("site_code") not in VALID_SITE:
                issues.append(f"card.site_code: invalid '{r.get('site_code')}'")

        cur.execute(
            "SELECT advert_purpose FROM t_advert_agent_purpose_score WHERE decision_id=%s",
            (did,),
        )
        for r in cur.fetchall():
            if r.get("advert_purpose") not in VALID_PURPOSE:
                issues.append(f"purpose_score.advert_purpose: invalid '{r.get('advert_purpose')}'")

        cur.execute("SELECT COUNT(*) n FROM t_advert_agent_modify_suggest_card WHERE decision_id=%s", (did,))
        cards = cur.fetchone()["n"]
        cur.execute(
            "SELECT SUM(keyword_id LIKE 'kwd%%') fake FROM t_advert_agent_modify_keyword_pending "
            "WHERE decision_id=%s",
            (did,),
        )
        fake_kw = int(cur.fetchone()["fake"] or 0)
        if fake_kw:
            issues.append(f"keyword_pending: {fake_kw} fake kwd% ids")

        print(
            f"cards={cards} expected={expected_cards} "
            f"uniq_campaign={uniq_campaign} kb_adjustments={len(adj)}"
        )
        if cards != expected_cards:
            issues.append(f"card count {cards} != expected {expected_cards}")
        if cards != uniq_campaign:
            issues.append(f"card count {cards} != kb unique campaign_id {uniq_campaign}")
        cur.execute(
            "SELECT campaign_id, COUNT(*) n FROM t_advert_agent_modify_suggest_card "
            "WHERE decision_id=%s GROUP BY campaign_id HAVING n > 1",
            (did,),
        )
        dup = cur.fetchall()
        if dup:
            issues.append(f"duplicate campaign_id on cards: {len(dup)} groups")
        if issues:
            print("ENUM ISSUES:")
            for i in issues:
                print(f"  - {i}")
            all_issues.extend(f"{asin}: {i}" for i in issues)
        else:
            print("ENUM CHECK: PASS")
        print()

    conn.close()
    if all_issues:
        print("VERDICT: FAIL")
        return 1
    print("VERDICT: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())

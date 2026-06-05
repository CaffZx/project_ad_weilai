"""Audit latest batch ERP writes — counts, Tab4 vs wizard, orphans."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pymysql

from _bootstrap import REPO_ROOT
from _erp_conn import erp_pymysql_conn

sys.path.insert(0, str(REPO_ROOT / "ad-direction-agent"))
from app.persistence.erp_writer.text_utils import (  # noqa: E402
    build_wizard_direction_content_json,
    map_direction_type,
)

OUT = REPO_ROOT / "cursor临时文件"
STATUS = OUT / "erp_batch_sequential_status.json"
VALID_DIR = {"PUSH_NATURAL", "EXPAND_KEYWORDS", "OPTIMIZE_ACOS", "BALANCE_MAINTAIN"}


def _load_jsonl(path: Path) -> dict | None:
    if not path.exists():
        return None
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            return json.loads(line)
    return None


def audit_one(cur, asin: str, did: str, exp_cards: int, wizard_path: Path) -> list[str]:
    issues: list[str] = []
    wizard = _load_jsonl(wizard_path)

    cur.execute(
        "SELECT parent_asin, advert_direction_types FROM t_advert_agent_decision WHERE id=%s",
        (did,),
    )
    dec = cur.fetchone()
    if not dec:
        return ["decision row missing"]
    if dec["parent_asin"] != asin:
        issues.append(f"parent_asin={dec['parent_asin']}")

    checks = [
        ("t_advert_agent_modify_suggest_card", exp_cards),
        ("t_advert_agent_modify_suggest_summary", 1),
        ("t_advert_agent_direction_recommend", 1),
        ("t_advert_agent_direction_recommend_detail", 4),
    ]
    for table, expected in checks:
        cur.execute(f"SELECT COUNT(1) AS n FROM {table} WHERE decision_id=%s", (did,))
        n = cur.fetchone()["n"]
        if n != expected:
            issues.append(f"{table}: {n} rows (expected {expected})")

    cur.execute(
        """
        SELECT COUNT(1) AS n FROM t_advert_agent_modify_keyword_pending k
        LEFT JOIN t_advert_agent_modify_suggest_card c
          ON k.suggest_card_id=c.id AND k.decision_id=c.decision_id
        WHERE k.decision_id=%s AND c.id IS NULL
        """,
        (did,),
    )
    if cur.fetchone()["n"]:
        issues.append("orphan keyword_pending rows")

    cur.execute(
        """
        SELECT direction_type, recommend_tag, suggest_score, content_json
        FROM t_advert_agent_direction_recommend_detail
        WHERE decision_id=%s ORDER BY sort_order
        """,
        (did,),
    )
    by_type = {r["direction_type"]: r for r in cur.fetchall()}
    for code in VALID_DIR:
        if code not in by_type:
            issues.append(f"missing direction_type {code}")

    if wizard and wizard.get("directions"):
        for d in wizard["directions"]:
            erp = map_direction_type(d.get("id") or "")
            row = by_type.get(erp)
            if not row:
                issues.append(f"wizard direction {d.get('id')} not in DB")
                continue
            score = int(float(d.get("suitability_score") or 0))
            if row["suggest_score"] != score:
                issues.append(f"{erp}: score {row['suggest_score']} != wizard {score}")
            if row["recommend_tag"] != d.get("suitability"):
                issues.append(f"{erp}: tag {row['recommend_tag']} != wizard {d.get('suitability')}")
            raw = (row["content_json"] or "").strip()
            if not raw.startswith("["):
                issues.append(f"{erp}: content_json not JSON array")
                continue
            try:
                body = json.loads(raw)
            except json.JSONDecodeError:
                issues.append(f"{erp}: content_json invalid JSON")
                continue
            expected = build_wizard_direction_content_json(d, {}, None)
            if body != expected:
                issues.append(f"{erp}: content_json lines mismatch wizard")

    types = json.loads(dec.get("advert_direction_types") or "[]")
    bad = [t for t in types if t not in VALID_DIR]
    if bad:
        issues.append(f"advert_direction_types legacy codes: {bad}")

    return issues


def main() -> int:
    if not STATUS.exists():
        print(f"Missing {STATUS}")
        return 1
    batch = json.loads(STATUS.read_text(encoding="utf-8"))
    results = batch.get("results") or []

    conn = pymysql.connect(**erp_pymysql_conn())
    cur = conn.cursor()
    total_issues = 0
    print("ERP BATCH WRITE AUDIT")
    print("=" * 72)
    for entry in results:
        asin = entry.get("asin") or ""
        did = entry.get("decision_id") or ""
        cards = int(entry.get("cards") or 0)
        wizard_path = OUT / f"{asin}_wizard.jsonl"
        issues = audit_one(cur, asin, did, cards, wizard_path)
        ok = entry.get("ok") and not issues
        status = "PASS" if ok else "FAIL"
        print(f"{asin} | {did} | cards={cards} | batch_ok={entry.get('ok')} | {status}")
        if issues:
            for i in issues:
                print(f"  - {i}")
            total_issues += len(issues)
        elif wizard_path.exists():
            w = _load_jsonl(wizard_path)
            for d in (w or {}).get("directions") or []:
                erp = map_direction_type(d.get("id") or "")
                print(f"  OK {erp}: {int(d.get('suitability_score') or 0)} {d.get('suitability')}")
    conn.close()
    print("=" * 72)
    if total_issues:
        print(f"FAILED: {total_issues} issue(s)")
        return 1
    print("ALL PASS — database writes match wizard + expected counts")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

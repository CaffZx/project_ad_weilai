"""Verify Tab4 content_json is semicolon-split string array (unit + optional ERP live)."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from _bootstrap import bootstrap_sys_path

bootstrap_sys_path()

from app.persistence.erp_writer.auto_push import _merge_decision_meta  # noqa: E402
from app.persistence.erp_writer.text_utils import (  # noqa: E402
    build_wizard_direction_content_json,
    normalize_advert_direction_types_list,
    split_text_to_semicolon_lines,
)


def _load_wizard(path: Path) -> dict:
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            return json.loads(line)
    raise ValueError(f"empty jsonl: {path}")


def _check_lines_body(body: list, d: dict, prefix: str) -> list[str]:
    issues: list[str] = []
    if not isinstance(body, list):
        issues.append(f"{prefix}: content_json must be JSON array")
        return issues
    if any(not isinstance(x, str) for x in body):
        issues.append(f"{prefix}: content_json array must be strings")
    expected = split_text_to_semicolon_lines(d.get("reason"))
    if body != expected:
        issues.append(f"{prefix}: lines mismatch wizard reason split")
    return issues


def test_from_wizard_file(wizard_path: Path) -> list[str]:
    issues: list[str] = []
    w = _load_wizard(wizard_path)
    directions = w.get("directions") or []
    validations = w.get("validations") or {}
    decisions = w.get("decisions") or {}

    if len(directions) != 4:
        issues.append(f"expected 4 directions, got {len(directions)}")

    for d in directions:
        dir_id = d.get("id") or ""
        pkg = (decisions.get(dir_id) or {}).get("decision_package")
        body = build_wizard_direction_content_json(
            d, validations.get(dir_id) or {}, pkg
        )
        issues.extend(_check_lines_body(body, d, dir_id))

    legacy = ["OPTIMIZE_ACOS", "BALANCE_MAINTENANCE", "ADD_KEYWORD_EXPANSION"]
    norm = normalize_advert_direction_types_list(legacy)
    if norm != ["OPTIMIZE_ACOS", "BALANCE_MAINTAIN", "EXPAND_KEYWORDS"]:
        issues.append(f"legacy normalize failed: {norm}")

    meta = _merge_decision_meta(
        {},
        {
            "decision_meta": {"advert_direction_types": legacy},
            "selected_directions": w.get("selected_directions"),
        },
    )
    if meta.get("advert_direction_types") != norm:
        issues.append(f"merge_decision_meta dirs: {meta.get('advert_direction_types')}")

    return issues


def test_erp_live(decision_id: str) -> list[str]:
    import pymysql

    from _erp_conn import erp_pymysql_conn

    issues: list[str] = []
    conn = pymysql.connect(**erp_pymysql_conn(connect_timeout=10))
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT direction_type, recommend_tag, suggest_score, sort_order, content_json
            FROM t_advert_agent_direction_recommend_detail
            WHERE decision_id=%s ORDER BY sort_order
            """,
            (decision_id,),
        )
        rows = cur.fetchall()
        if len(rows) != 4:
            issues.append(f"ERP: expected 4 rows, got {len(rows)}")
        for r in rows:
            raw = r.get("content_json") or ""
            if not raw.strip().startswith("["):
                issues.append(f"{r['direction_type']}: content_json not JSON array")
                continue
            try:
                body = json.loads(raw)
            except json.JSONDecodeError:
                issues.append(f"{r['direction_type']}: invalid JSON")
                continue
            if not isinstance(body, list):
                issues.append(f"{r['direction_type']}: content_json must be array")
            elif any(not isinstance(x, str) for x in body):
                issues.append(f"{r['direction_type']}: array items must be strings")
        cur.execute(
            "SELECT advert_direction_types FROM t_advert_agent_decision WHERE id=%s",
            (decision_id,),
        )
        dec = cur.fetchone()
        if dec:
            types = json.loads(dec.get("advert_direction_types") or "[]")
            bad = [t for t in types if t.endswith("ANCE") or t == "ADD_KEYWORD_EXPANSION"]
            if bad:
                issues.append(f"decision advert_direction_types has legacy codes: {bad}")
    finally:
        conn.close()
    return issues


def main() -> int:
    p = argparse.ArgumentParser(description="Verify Tab4 content_json string array")
    p.add_argument("--wizard", type=Path, help="Wizard jsonl for offline test")
    p.add_argument("--decision-id", help="ERP decision_id for live DB check")
    args = p.parse_args()

    all_issues: list[str] = []
    print("=== Tab4 content_json verification ===\n")

    if args.wizard:
        print(f"[offline] wizard: {args.wizard}")
        all_issues.extend(test_from_wizard_file(args.wizard))
        if not all_issues:
            w = _load_wizard(args.wizard)
            for d in w.get("directions") or []:
                lines = build_wizard_direction_content_json(
                    d,
                    (w.get("validations") or {}).get(d["id"]),
                    ((w.get("decisions") or {}).get(d["id"]) or {}).get(
                        "decision_package"
                    ),
                )
                print(f"  OK {d['id']}: {len(lines)} line(s) -> {lines[0][:40]!r}…")

    if args.decision_id:
        print(f"\n[ERP live] decision_id: {args.decision_id}")
        live_issues = test_erp_live(args.decision_id)
        all_issues.extend(live_issues)
        if not live_issues:
            import pymysql
            from _erp_conn import erp_pymysql_conn

            conn = pymysql.connect(**erp_pymysql_conn(connect_timeout=10))
            cur = conn.cursor()
            cur.execute(
                """
                SELECT direction_type, suggest_score, recommend_tag, content_json
                FROM t_advert_agent_direction_recommend_detail
                WHERE decision_id=%s ORDER BY sort_order
                """,
                (args.decision_id,),
            )
            for r in cur.fetchall():
                lines = json.loads(r["content_json"] or "[]")
                print(
                    f"  OK {r['direction_type']}: "
                    f"{r['recommend_tag']} | {r['suggest_score']}分 | {len(lines)} lines"
                )
            conn.close()

    if all_issues:
        print("\nFAILED:")
        for i in all_issues:
            print(f"  - {i}")
        return 1
    print("\nALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())

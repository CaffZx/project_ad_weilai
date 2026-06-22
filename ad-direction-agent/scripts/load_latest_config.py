"""从 t_advert_agent_decision_config 取父 ASIN 的「最新配置」——逐字段取最近一次非空值。

背景（见 Campaign 交接文档 §19/§21 R2）：config 表按 decision_id 逐批次追加，同一 ASIN
有多行；某字段的最近非空值可能散落在不同 decision_id 的记录里。定时轨需要的不是「最新一行」，
而是「逐字段跨行取最近非空」拼装出的有效配置。

本文件双用途：
  1. `load_latest_config(cur, parent_asin, parent_seller_sku=None)` —— R2 参考实现，
     将来折进 `app/data/decision_config_reader.load_layer14`。
  2. CLI —— 对 prod 只读跑，核对某 ASIN（或全部）拼出来的配置 + 每字段来源 decision_id。

只读，不写库。运行：
  cd ad-direction-agent
  PYTHONPATH=. .venv/bin/python3.11 scripts/load_latest_config.py B0B7S3PWWB
  PYTHONPATH=. .venv/bin/python3.11 scripts/load_latest_config.py --all --json
"""

from __future__ import annotations

import argparse
import json
import sys

# 参与「逐字段取最近非空」的配置字段。值字段(acos/budget)只判 NULL；
# 文本/多值字段额外把 '' 和 '[]' 视为空。
_CONFIG_FIELDS = [
    "product_position",
    "product_stage",
    "season_type",
    "advert_purposes",
    "target_keyword_types",
    "advert_direction_types",
    "day_range",
    "target_acos_suggest",
    "daily_budget_suggest",
]
# 随最新一行带出的身份字段（不参与「最近非空」拼装，取最新行即可）
_IDENTITY_FIELDS = ["shop_id", "parent_seller_sku", "site_code"]

_EMPTY_STRINGS = {"", "[]", "null", "NULL"}


def _is_empty(value) -> bool:
    if value is None:
        return True
    if isinstance(value, str) and value.strip() in _EMPTY_STRINGS:
        return True
    return False


def load_latest_config(cur, parent_asin: str, parent_seller_sku: str | None = None) -> dict:
    """逐字段取该 ASIN 最近一次非空配置值（可跨 decision_id）。

    返回：
      {
        "parent_asin": ...,
        "config": {field: value, ...},            # 拼装结果，缺失字段为 None
        "provenance": {field: {"decision_id","update_time"}},  # 每字段取自哪一行
        "missing": [field, ...],                   # 全无非空历史的字段（定时轨需 AI 兜底）
        "row_count": N,                            # 该 ASIN 的行数
      }
    """
    asin = (parent_asin or "").strip()
    if not asin:
        return {"parent_asin": "", "config": {}, "provenance": {}, "missing": _CONFIG_FIELDS[:], "row_count": 0}

    cols = ", ".join(["id", "update_time", *_IDENTITY_FIELDS, *_CONFIG_FIELDS])
    if parent_seller_sku:
        cur.execute(
            f"SELECT {cols} FROM t_advert_agent_decision_config "
            "WHERE parent_asin=%s AND parent_seller_sku=%s "
            "ORDER BY update_time DESC, id DESC",
            (asin, parent_seller_sku),
        )
    else:
        cur.execute(
            f"SELECT {cols} FROM t_advert_agent_decision_config "
            "WHERE parent_asin=%s ORDER BY update_time DESC, id DESC",
            (asin,),
        )
    rows = cur.fetchall()

    config: dict = {f: None for f in _CONFIG_FIELDS}
    provenance: dict = {}
    if not rows:
        return {"parent_asin": asin, "config": config, "provenance": {},
                "missing": _CONFIG_FIELDS[:], "row_count": 0}

    # 身份字段：取最新行（rows[0]，已按 update_time DESC 排序）
    latest = rows[0]
    for f in _IDENTITY_FIELDS:
        config[f] = latest.get(f)

    # 逐字段：从最新行往旧行扫，命中第一个非空即停（=最近一次非空）
    for field in _CONFIG_FIELDS:
        for row in rows:
            if not _is_empty(row.get(field)):
                val = row.get(field)
                # Decimal → float，JSON 安全
                try:
                    from decimal import Decimal
                    if isinstance(val, Decimal):
                        val = float(val)
                except Exception:  # noqa: BLE001
                    pass
                config[field] = val
                provenance[field] = {
                    "decision_id": row.get("id"),
                    "update_time": str(row.get("update_time")),
                }
                break

    missing = [f for f in _CONFIG_FIELDS if _is_empty(config.get(f))]
    return {
        "parent_asin": asin,
        "config": config,
        "provenance": provenance,
        "missing": missing,
        "row_count": len(rows),
    }


def _all_parent_asins(cur) -> list[str]:
    cur.execute("SELECT DISTINCT parent_asin FROM t_advert_agent_decision_config ORDER BY parent_asin")
    return [r["parent_asin"] for r in cur.fetchall() if r.get("parent_asin")]


def _print_human(res: dict) -> None:
    print(f"\n=== {res['parent_asin']}  (rows={res['row_count']}) ===")
    spans = set(p["decision_id"] for p in res["provenance"].values())
    if len(spans) > 1:
        print(f"  ⚠ 配置散落在 {len(spans)} 个 decision_id 中（逐字段最近非空拼装）")
    for f in _CONFIG_FIELDS:
        v = res["config"].get(f)
        src = res["provenance"].get(f)
        src_s = f"  <- {src['decision_id'][:12]}.. @ {src['update_time'][:10]}" if src else "  <- (无非空历史)"
        print(f"  {f:24} = {('NULL' if _is_empty(v) else v)!s:<22}{src_s}")
    if res["missing"]:
        print(f"  缺失(需AI兜底): {', '.join(res['missing'])}")


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="逐字段取父ASIN最近非空配置（定时轨 R2 参考实现）")
    ap.add_argument("asin", nargs="?", help="父 ASIN（不传则配合 --all）")
    ap.add_argument("--sku", default=None, help="父 seller SKU（可选，更精确）")
    ap.add_argument("--all", action="store_true", help="跑全部 ASIN")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--only-incomplete", action="store_true", help="仅显示有缺失字段的 ASIN")
    args = ap.parse_args(argv)

    if not args.asin and not args.all:
        ap.error("需要传 asin 或 --all")

    from app.persistence.erp_writer.repository import _get_repository
    conn = _get_repository()._connect()
    try:
        cur = conn.cursor()
        asins = _all_parent_asins(cur) if args.all else [args.asin]
        results = [load_latest_config(cur, a, args.sku) for a in asins]
    finally:
        conn.close()

    if args.only_incomplete:
        results = [r for r in results if r["missing"]]

    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2, default=str))
    else:
        for r in results:
            _print_human(r)
        print(f"\n总计 {len(results)} 个 ASIN" + ("（仅列缺失）" if args.only_incomplete else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

"""跨库数据迁移：改造前正式库(旧结构) → 改造后正式库(= 测试库结构)。

同一台 MySQL 服务器、两个 schema。逐表生成「结构感知」的 INSERT...SELECT。

策略（依据 docs/ERP数据库改造方案.md 与 docs/ERP测试库vs正式库结构对比.md）：
  - 每表迁移列 = 源列 ∩ 目标列：
      * 源有目标无的列（如 record.version）自动丢弃；
      * 目标有源无的新列（is_latest / create_count / shop_id ...）不写 → 走 DDL 默认值。
  - 共有列中「目标为数值、源为字符串」者，包 CAST(NULLIF(TRIM(col),'') AS <num>)，
    以兼容 strict sql_mode（空串直插会报错）。
  - 特殊规则：placement_type 缩写→全名；purpose_score 排除 Clearance 脏行；
    decision.parent_asin 存中文名时复制到新列 product_name（原 ASIN 列待人工回填）。
  - 装载期 FOREIGN_KEY_CHECKS=0；逐表提交；最后回填 is_latest（窗口函数）。

默认 dry-run：只打印将执行的 SQL。加 --execute 才真正写库。--out 把 SQL 落到文件供 DBA 复核。

用法（在 ad-direction-agent 目录下）：
  # 预览（不写库）
  python scripts/erp_db/migrate_prod_data.py \
      --host <prod_host> --user <u> --password <p> \
      --src-db <旧库schema> --dst-db <新库schema> --out /tmp/migrate.sql

  # 真正执行
  python scripts/erp_db/migrate_prod_data.py \
      --host <prod_host> --user <u> --password <p> \
      --src-db <旧库schema> --dst-db <新库schema> --execute
"""
from __future__ import annotations

import argparse
import sys

import pymysql
from pymysql.cursors import DictCursor

# 父表在前、子表在后（FK 关掉后顺序其实无关，仅为可读性）。
# core_keyword_tracking / portfolio_record 默认一并迁移（保守不丢数据）；
# 如确认废弃，从此列表删除即可。
TABLES = [
    "t_advert_agent_decision",
    "t_advert_agent_decision_config",
    "t_advert_agent_purpose_score",
    "t_advert_agent_direction_recommend",
    "t_advert_agent_direction_recommend_detail",
    "t_advert_agent_ai_suggest",
    "t_advert_agent_data_metrics",
    "t_advert_agent_core_keyword_tracking",
    "t_advert_agent_modify_suggest_summary",
    "t_advert_agent_modify_suggest_card",
    "t_advert_agent_modify_keyword_pending",
    "t_advert_agent_modify_campaign_pending",
    "t_advert_agent_modify_placement_pending",
    "t_advert_agent_modify_advert_record",
    "t_advert_agent_modify_campaign_record",
    "t_advert_agent_modify_keyword_record",
    "t_advert_agent_modify_placement_record",
    "t_advert_agent_modify_portfolio_record",
]

NUMERIC_TYPES = {"tinyint", "smallint", "mediumint", "int", "bigint",
                 "decimal", "double", "float"}
STRING_TYPES = {"char", "varchar", "tinytext", "text", "mediumtext",
                "longtext", "enum", "set"}

# 每表特殊处理：
#   where     —— 迁移过滤（排脏行）
#   overrides —— 用自定义 SQL 表达式替换某共有列的取值
#   extra     —— 目标独有、但希望主动填充的列（否则走默认）；表达式引用源列
SPECIAL: dict[str, dict] = {
    "t_advert_agent_purpose_score": {
        "where": "advert_purpose IS NULL OR advert_purpose <> 'Clearance'",
    },
    "t_advert_agent_modify_placement_record": {
        "overrides": {
            "placement_type": (
                "CASE `placement_type` "
                "WHEN 'TOP' THEN 'TOP_OF_SEARCH' "
                "WHEN 'REST' THEN 'REST_OF_SEARCH' "
                "ELSE `placement_type` END"
            ),
        },
    },
    "t_advert_agent_decision": {
        "extra": {
            # parent_asin 存中文名(非 B0)时，保留到 product_name；原列保持原样待人工回填
            "product_name": "CASE WHEN `parent_asin` NOT LIKE 'B0%' THEN `parent_asin` END",
        },
    },
}


def fetch_columns(cur, schema: str, table: str) -> dict[str, dict]:
    """返回 {col_name: meta}，按 ORDINAL_POSITION 有序。"""
    cur.execute(
        """
        SELECT COLUMN_NAME, DATA_TYPE, NUMERIC_PRECISION, NUMERIC_SCALE,
               IS_NULLABLE, COLUMN_DEFAULT, EXTRA, GENERATION_EXPRESSION
        FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s
        ORDER BY ORDINAL_POSITION
        """,
        (schema, table),
    )
    return {r["COLUMN_NAME"]: r for r in cur.fetchall()}


def cast_expr(col: str, src_meta: dict, dst_meta: dict) -> str:
    """共有列的 SELECT 表达式：仅在 目标数值 & 源字符串 时做 CAST。"""
    q = f"`{col}`"
    dst_t = (dst_meta["DATA_TYPE"] or "").lower()
    src_t = (src_meta["DATA_TYPE"] or "").lower()
    if dst_t in NUMERIC_TYPES and src_t in STRING_TYPES:
        inner = f"NULLIF(TRIM({q}),'')"
        if dst_t == "decimal":
            p = dst_meta["NUMERIC_PRECISION"] or 18
            s = dst_meta["NUMERIC_SCALE"] or 0
            return f"CAST({inner} AS DECIMAL({p},{s}))"
        return f"CAST({inner} AS SIGNED)"  # int 家族 / bigint
    return q


def risky_dst_only(dst_meta: dict) -> bool:
    """目标独有列若 NOT NULL 且无默认、非自增、非生成列 → 省略它会插入失败。"""
    if dst_meta["IS_NULLABLE"] == "YES":
        return False
    if dst_meta["COLUMN_DEFAULT"] is not None:
        return False
    extra = (dst_meta["EXTRA"] or "").lower()
    if "auto_increment" in extra or "generated" in extra:
        return False
    return True


def build_table_sql(cur, src_db: str, dst_db: str, table: str) -> tuple[str | None, list[str]]:
    """生成单表 INSERT...SELECT；返回 (sql 或 None, 警告列表)。"""
    warnings: list[str] = []
    src_cols = fetch_columns(cur, src_db, table)
    dst_cols = fetch_columns(cur, dst_db, table)
    if not src_cols:
        return None, [f"[skip] 源库无表 {table}"]
    if not dst_cols:
        return None, [f"[skip] 目标库无表 {table}"]

    spec = SPECIAL.get(table, {})
    overrides: dict[str, str] = spec.get("overrides", {})
    extra: dict[str, str] = spec.get("extra", {})
    where: str | None = spec.get("where")

    insert_cols: list[str] = []
    select_exprs: list[str] = []

    # 1) 共有列（按目标列顺序），自动丢弃源独有列（version 等）
    for name, dst_meta in dst_cols.items():
        if name not in src_cols:
            continue
        insert_cols.append(f"`{name}`")
        if name in overrides:
            select_exprs.append(overrides[name])
        else:
            select_exprs.append(cast_expr(name, src_cols[name], dst_meta))

    # 2) 目标独有列：默认跳过(走默认值)；extra 中声明的主动填充；NOT NULL 无默认的报警
    for name, dst_meta in dst_cols.items():
        if name in src_cols:
            continue
        if name in extra:
            insert_cols.append(f"`{name}`")
            select_exprs.append(extra[name])
            continue
        if risky_dst_only(dst_meta):
            warnings.append(
                f"[warn] {table}.{name} 是目标独有的 NOT NULL 列且无默认值，"
                f"省略将导致 INSERT 失败 —— 需在 SPECIAL[{table!r}]['extra'] 提供取值或给列加默认。"
            )

    cols_sql = ", ".join(insert_cols)
    exprs_sql = ",\n       ".join(select_exprs)
    sql = (
        f"INSERT INTO `{dst_db}`.`{table}` ({cols_sql})\n"
        f"SELECT {exprs_sql}\n"
        f"FROM `{src_db}`.`{table}`"
    )
    if where:
        sql += f"\nWHERE {where}"
    sql += ";"
    return sql, warnings


def is_latest_backfill_sql(dst_db: str) -> list[str]:
    """复用 migrate_decision_lifecycle.sql 的回填逻辑（清 0 + 每 ASIN 取最新置 1）。"""
    t = f"`{dst_db}`.`t_advert_agent_decision`"
    return [
        f"UPDATE {t} SET is_latest = 0;",
        f"""UPDATE {t} d
JOIN (
    SELECT id FROM (
        SELECT id, ROW_NUMBER() OVER (
            PARTITION BY parent_asin ORDER BY create_time DESC, id DESC) AS rn
        FROM {t}
    ) ranked WHERE rn = 1
) pick ON pick.id = d.id
SET d.is_latest = 1;""",
    ]


def table_count(cur, schema: str, table: str) -> int:
    cur.execute(f"SELECT COUNT(*) AS c FROM `{schema}`.`{table}`")
    return cur.fetchone()["c"]


def main() -> int:
    ap = argparse.ArgumentParser(description="跨库迁移 改造前→改造后 正式库数据")
    ap.add_argument("--host", required=True)
    ap.add_argument("--port", type=int, default=3306)
    ap.add_argument("--user", required=True)
    ap.add_argument("--password", required=True)
    ap.add_argument("--src-db", required=True, help="改造前正式库 schema 名")
    ap.add_argument("--dst-db", required=True, help="改造后正式库 schema 名(= 测试库结构)")
    ap.add_argument("--execute", action="store_true", help="真正写库；缺省仅 dry-run 打印 SQL")
    ap.add_argument("--force", action="store_true", help="目标表非空时仍继续（默认拒绝）")
    ap.add_argument("--out", help="把生成的 SQL 写入该文件")
    ap.add_argument("--only", nargs="*", help="只处理这些表（调试用）")
    args = ap.parse_args()

    if args.src_db == args.dst_db:
        print("src-db 与 dst-db 不能相同", file=sys.stderr)
        return 2

    conn = pymysql.connect(
        host=args.host, port=args.port, user=args.user, password=args.password,
        charset="utf8mb4", cursorclass=DictCursor, connect_timeout=15,
    )
    tables = args.only or TABLES
    generated: list[tuple[str, str]] = []  # (table, sql)
    all_warnings: list[str] = []
    try:
        with conn.cursor() as cur:
            # 预检：目标表非空则中止（除非 --force）
            nonempty = []
            for t in tables:
                try:
                    if table_count(cur, args.dst_db, t) > 0:
                        nonempty.append(t)
                except pymysql.err.ProgrammingError:
                    pass  # 表不存在，build 阶段会报 skip
            if nonempty and not args.force:
                print("目标库以下表非空，拒绝迁移（加 --force 覆盖语义需自行 TRUNCATE）：",
                      file=sys.stderr)
                for t in nonempty:
                    print(f"  - {t}", file=sys.stderr)
                return 3

            for t in tables:
                sql, warns = build_table_sql(cur, args.src_db, args.dst_db, t)
                all_warnings.extend(warns)
                for w in warns:
                    print(w, file=sys.stderr)
                if sql:
                    generated.append((t, sql))

            if args.out:
                with open(args.out, "w", encoding="utf-8") as f:
                    f.write("SET FOREIGN_KEY_CHECKS=0;\nSET UNIQUE_CHECKS=0;\n\n")
                    for t, sql in generated:
                        f.write(f"-- ===== {t} =====\n{sql}\n\n")
                    f.write("-- ===== is_latest 回填 =====\n")
                    for s in is_latest_backfill_sql(args.dst_db):
                        f.write(s + "\n")
                    f.write("\nSET UNIQUE_CHECKS=1;\nSET FOREIGN_KEY_CHECKS=1;\n")
                print(f"[out] 生成 SQL 已写入 {args.out}")

            if not args.execute:
                print("\n===== DRY-RUN（未写库）=====")
                for t, sql in generated:
                    print(f"\n-- {t}\n{sql}")
                if all_warnings:
                    print(f"\n⚠ {len(all_warnings)} 条警告，见上 stderr。")
                print("\n加 --execute 才真正执行。")
                return 0

            # ---- 真正执行 ----
            if all_warnings and not args.force:
                print("存在警告（目标 NOT NULL 列无处取值），加 --force 才允许带警告执行。",
                      file=sys.stderr)
                return 4
            cur.execute("SET FOREIGN_KEY_CHECKS=0")
            cur.execute("SET UNIQUE_CHECKS=0")
            for t, sql in generated:
                cur.execute(sql)
                conn.commit()
                print(f"loaded {t}: {cur.rowcount} rows")
            print("backfill is_latest ...")
            for s in is_latest_backfill_sql(args.dst_db):
                cur.execute(s)
            conn.commit()
            cur.execute("SET UNIQUE_CHECKS=1")
            cur.execute("SET FOREIGN_KEY_CHECKS=1")

            # ---- 校验：源 vs 目标行数 ----
            print("\n===== 行数校验（源 → 目标）=====")
            for t, _ in generated:
                try:
                    sc = table_count(cur, args.src_db, t)
                    dc = table_count(cur, args.dst_db, t)
                    flag = "" if sc == dc else "  ← 差异(检查清洗/排除行)"
                    print(f"  {t}: {sc} → {dc}{flag}")
                except pymysql.err.ProgrammingError:
                    pass
    finally:
        conn.close()
    print("\n完成。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

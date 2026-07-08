"""Campaign 预筛选 — 纯函数，无 I/O。

硬过滤规则（排除不进 LLM）:
  - 非 ENABLED 状态
  - 近 7 天完全无数据
  - 多关键词活动 (本期暂不处理，后续迭代启用 suggest_split)
"""

from __future__ import annotations

from collections import Counter


def filter_campaigns(raw: list[dict]) -> tuple[list[dict], list[dict]]:
    """返回 (surviving, excluded)。

    硬过滤:
    1. campaign_status != 'ENABLED' 或 keyword_status != 'ENABLED'
       → reason="inactive"
    2. 近 7 天 spend=0 AND clicks=0 AND impressions=0 (完全无数据)
       → reason="no_recent_data"
    3. campaign_name 下 COUNT(DISTINCT keyword_text) > 1
       → reason="multi_keyword_deferred"
       # 本期暂不处理批量关键词活动，后续迭代启用 suggest_split

    注意: budget=$1 的活动不在此排除 -- 留给 LLM 按 KB 规则判断淘汰保护条件。
    """
    # Step 1: 按 campaign_id 统计 ① 去重投放关键词数（排除否定词）② 各子ASIN关联行数
    #         改用 campaign_id 而非 campaign_name：id 是数字主键，跨工具稳定。
    #         否定词（negativeExact/negativePhrase）不计入多词判定——广泛活动常有同词根否词，
    #         若把否词算进去会导致误判为"多关键词活动"。
    def _is_negative_match(mt: str) -> bool:
        return "negative" in (mt or "").lower()

    cid_kw_count: dict[str, set] = {}
    cid_asin_rows: dict[str, Counter] = {}
    cid_name: dict[str, str] = {}  # cid → name（取首次出现的名称）
    for r in raw:
        cid = str(r.get("campaign_id") or "")
        kw = str(r.get("keyword_text") or "")
        mt = str(r.get("关键词匹配类型") or r.get("keyword_match_type") or "")
        ca = str(r.get("child_asin") or "")
        name = str(r.get("campaign_name") or "")
        if cid and kw and not _is_negative_match(mt):
            cid_kw_count.setdefault(cid, set()).add(kw)
        if cid and ca:
            cid_asin_rows.setdefault(cid, Counter())[ca] += 1
        if cid and cid not in cid_name:
            cid_name[cid] = name

    multi_kw_cids = {cid for cid, kws in cid_kw_count.items() if len(kws) > 1}

    surviving: list[dict] = []
    excluded: list[dict] = []
    emitted_multi: set[str] = set()   # 多词活动只产 1 条 excluded（防同活动 N 行重复）

    for r in raw:
        name = str(r.get("campaign_name") or "")
        cid = str(r.get("campaign_id") or "")

        # 规则 1: 非活跃状态
        # 注意：当前 _fetch_campaign_context SQL 已用 WHERE campaign_status='ENABLED' 过滤，
        # 且输出列写死 'ENABLED'，故 raw 不含非 ENABLED 活动 → 本分支为死代码。
        # 若将来要展示非 ENABLED，需改 SQL（去 WHERE + 取真实 status），届时本分支转活。
        campaign_status = str(r.get("campaign_status") or "").upper()
        keyword_status = str(r.get("keyword_status") or "").upper()
        if campaign_status != "ENABLED" or (keyword_status and keyword_status != "ENABLED"):
            excluded.append({"campaign_name": name, "reason": "inactive"})
            continue

        # 规则 2: 近 7 天无数据 — 死代码（同规则1，SQL 的 local_report_time>=7d 已隐式排除；
        #         且 report 表对零活动无行，无 campaign 主表可 LEFT JOIN，本期不展示该类）。
        if "spend_7d" in r or "clicks_7d" in r or "impressions_7d" in r:
            spend_7d = float(r.get("spend_7d") or r.get("cost_7d") or 0)
            clicks_7d = int(float(r.get("clicks_7d") or 0))
            impressions_7d = int(float(r.get("impressions_7d") or 0))
            if spend_7d == 0 and clicks_7d == 0 and impressions_7d == 0:
                excluded.append({"campaign_name": name, "reason": "no_recent_data"})
                continue

        # 规则 3: 多关键词活动（本期不进 LLM，前端展示为预过滤卡）
        #   - 折叠：每活动只产 1 条 excluded（raw 按词/子ASIN 多行，否则会 N 条重复）
        #   - 子ASIN：取该活动下关联词条数最多的子ASIN（prefilter 无 perf，"花费最高"不可得）
        #   - keyword_text 硬编码"多关键词活动"
        if cid in multi_kw_cids:
            if cid not in emitted_multi:
                emitted_multi.add(cid)
                top_asin = ""
                if cid_asin_rows.get(cid):
                    top_asin = cid_asin_rows[cid].most_common(1)[0][0]
                excluded.append({
                    "campaign_name": name,
                    "campaign_id": cid,
                    "child_asin": top_asin,
                    "match_type": str(r.get("match_type") or ""),
                    "keyword_text": "多关键词活动",
                    "keyword_count": len(cid_kw_count[cid]),
                    "reason": "多关键词活动，请到ERP手动修改",
                    "__prefiltered": True,
                })
            continue

        surviving.append(r)

    return surviving, excluded

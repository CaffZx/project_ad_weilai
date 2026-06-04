"""Campaign 预筛选 — 纯函数，无 I/O。

硬过滤规则（排除不进 LLM）:
  - 非 ENABLED 状态
  - 近 7 天完全无数据
  - 多关键词活动 (本期暂不处理，后续迭代启用 suggest_split)
"""

from __future__ import annotations


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
    # Step 1: 按 campaign_name 分组统计关键词数
    name_kw_count: dict[str, set] = {}
    for r in raw:
        name = str(r.get("campaign_name") or "")
        kw = str(r.get("keyword_text") or "")
        if name and kw:
            name_kw_count.setdefault(name, set()).add(kw)

    multi_kw_names = {n for n, kws in name_kw_count.items() if len(kws) > 1}

    surviving: list[dict] = []
    excluded: list[dict] = []

    for r in raw:
        name = str(r.get("campaign_name") or "")

        # 规则 1: 非活跃状态
        campaign_status = str(r.get("campaign_status") or "").upper()
        keyword_status = str(r.get("keyword_status") or "").upper()
        if campaign_status != "ENABLED" or (keyword_status and keyword_status != "ENABLED"):
            excluded.append({"campaign_name": name, "reason": "inactive"})
            continue

        # 规则 2: 近 7 天无数据 (spend + clicks + impressions 全为零)
        # 注意：当前上下文查询(_fetch_campaign_context)只取维度、不带 perf 字段，
        # 故此分支为防御性死代码——"近7天无数据"已由该 SQL 的
        # `local_report_time >= 7d` WHERE 隐式排除。保留以备将来上下文查询带指标。
        if "spend_7d" in r or "clicks_7d" in r or "impressions_7d" in r:
            spend_7d = float(r.get("spend_7d") or r.get("cost_7d") or 0)
            clicks_7d = int(float(r.get("clicks_7d") or 0))
            impressions_7d = int(float(r.get("impressions_7d") or 0))
            if spend_7d == 0 and clicks_7d == 0 and impressions_7d == 0:
                excluded.append({"campaign_name": name, "reason": "no_recent_data"})
                continue

        # 规则 3: 多关键词活动 (本期暂过滤)
        if name in multi_kw_names:
            excluded.append({
                "campaign_name": name,
                "reason": "multi_keyword_deferred",
                "keyword_count": len(name_kw_count[name]),
            })
            continue

        surviving.append(r)

    return surviving, excluded

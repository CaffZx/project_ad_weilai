# agent_router.py
import json
import os
import sys
import time
from pathlib import Path

from config import DEEPSEEK_BASE_URL, DEEPSEEK_MODEL, SIBLING_PROJECT
from openai import OpenAI

# Access unified data layer (ad-direction-agent's DbAdapter) — for determine_ad_targets only
_sys_root = Path(__file__).resolve().parent.parent / SIBLING_PROJECT
if str(_sys_root) not in sys.path:
    sys.path.insert(0, str(_sys_root))

from app.data.db_adapter import DbAdapter           # noqa: E402
from app.data.field_mapping import asin_data_to_metrics  # noqa: E402
from app.llm.key_pool import ApiKeyPool             # noqa: E402
from app.llm.client import key_pool                 # noqa: E402


async def determine_ad_targets_from_metrics(
    metrics: dict, position: str, stage: str, season: str,
    days: int = 7, keywords: list | None = None,
    trend_history: list | None = None,
    competitor_price: float | None = None,
    stock_days: int | None = None,
) -> dict:
    """AI diagnosis — 使用预先组装好的 metrics，不查数据库。

    由 direction-agent 的 purpose_adapter 负责查库并传入 metrics。
    本函数只负责 prompt 构建 + 知识库加载 + LLM 调用。
    """
    import time as _time
    gross_profit = float(metrics.get("unit_gross_profit") or 0)
    cpc = float(metrics.get("cpc") or 0)
    cpc_warning = "[ALERT: CPC exceeds margin!]" if gross_profit > 0 and cpc > gross_profit else "(safe)"

    orders_total = int(metrics.get("total_orders") or 0)
    _stock_days = stock_days
    if _stock_days is None:
        if orders_total > 0:
            daily_orders = orders_total / float(days)
            _stock_days = int(metrics.get("total_inventory", 0) / daily_orders)
        else:
            _stock_days = 999 if metrics.get("total_inventory", 0) > 0 else 0

    _keywords = keywords or metrics.get("top_keywords", [])
    _trend = trend_history or []

    # Build keyword trend string for AI prompt
    kw_trend_str = ""
    if _keywords:
        for kw in _keywords:
            sp_info = f" | SP广告位: 第{kw.get('sp_rank', 0)}名" if kw.get("sp_rank") else ""
            rank = kw.get("rank")
            if rank is None:
                rank_str = "自然排名: 未入榜"
                trend_label = "无排名数据"
            else:
                rank_str = f"自然排名第{rank}名"
                chg = kw.get("rank_change_7d", kw.get("rank_change_14d", 0)) or 0
                if chg >= 5:
                    trend_label = "快速上升"
                elif chg >= 1:
                    trend_label = "缓慢上升"
                elif chg <= -5:
                    trend_label = "快速下滑"
                elif chg <= -1:
                    trend_label = "轻微下滑"
                else:
                    trend_label = "持平"
            kw_trend_str += (
                f"    - [{kw['word']}]: "
                f"{rank_str}"
                f"{sp_info}"
                f" | 7d趋势: {trend_label}\n"
            )
    else:
        kw_trend_str = "    - No core keyword data\n"

    refund_rate_pct = float(metrics.get("refund_rate", 0)) * 100
    avg_price = float(metrics.get("avg_price", 0))
    _comp_price = competitor_price or avg_price * 0.95

    user_prompt = f"""
Please strictly follow the knowledge base rules to diagnose this ASIN:

[Current Product Data] (All below are {days}-day window aggregates unless noted otherwise)
- 产品定位: {position}
- 产品阶段: {stage}
- 淡旺季: {season}
- {days}日平均自然排名: {(metrics.get('avg_nature_rank') or 100):.1f}
- {days}日平均评分: {(metrics.get('avg_star') or 4.0):.1f}
- 当前售价: ${avg_price:.2f} (竞品参考价: ${_comp_price:.2f})
- {days}日平均净CVR: {((metrics.get('net_cvr') or 0) * 100):.2f}%
- {days}日平均CTR: {((metrics.get('ctr') or 0) * 100):.2f}%
- {days}日自然单占比: {((metrics.get('natural_order_ratio') or 0) * 100):.2f}% (总订单: {orders_total}, 广告订单: {metrics.get('ad_orders', 0)})
- {days}日平均ACOS: {((metrics.get('avg_acos') or 0) * 100):.2f}%
- {days}日平均CPC: ${cpc:.2f} | 单均毛利: ${gross_profit:.2f} {cpc_warning}
- {days}日退款率: {refund_rate_pct:.1f}%
- 当前可售库存天数: {_stock_days}天

[Core Keywords & Positions]:
(Note: For each keyword, determine its strategy type based on the Keyword Type Knowledge Base: Broad / Long-tail / Competitor / Brand / Custom)
{kw_trend_str}

[Last {days}d Daily Trend Data]
{_trend}

[Core Instructions]
1. `targets` array: ONLY targets with score >= 50. Sorted by score descending. If none qualify, return [].
2. `target_scores` array: ALL 5 targets (Traffic/Conversion/Ranking/Profit/Clearance) with their scores and detailed reasons. Even for low-score or excluded targets, you MUST write a full three-section reason — do NOT write short dismissals like "不适用" or "不推荐". Explain WHY specifically. The `reason` field MUST contain three sections using 【】 markers:

   【决策依据】— The most important part. Follow these rules:
	     (a) PRIMARY: trending daily data (from [Last {days}d Daily Trend Data]). Look at whether each metric is improving, deteriorating, or flat over the {days}d window. The TREND direction matters more than the static aggregate. Example: a 7d avg ACOS of XX% that dropped from [Day1 ACOS] → [Day7 ACOS] over the week is very different from a flat XX% — the former suggests rapid improvement, the latter suggests stagnation.
	     (b) SECONDARY: static window aggregates ({days}d avg CVR, {days}d avg ACOS, {days}d avg CPC, {days}d natural order ratio, etc.) as supporting evidence.
	     (c) EVERY metric MUST carry its time window AND caliber (某日当天值 or N日平均值). Write "{days}日平均ACOS [XX]%" not "ACOS [XX]%". For a specific date from trend: "[5月17日]当天ACOS [XX]%". For a trend range: "近{days}日ACOS从[5月12日]的[XX]%逐日降至[5月17日]的[XX]%". For a window average: "{days}日平均CVR [XX]%". (All bracketed values are placeholders — use the actual data from the prompt above.) Never drop the window or caliber.
	     (d) Only cite the 1-2 indicators that truly DETERMINED this score. Ignore secondary factors.

   【建议】— 1-2 sentences of concrete action recommendation. Say WHAT to do and WHY, not HOW.

   【后续关注】— 1-2 key indicators or conditions to monitor going forward. Under what circumstances should the recommendation strength for this direction be re-evaluated.

   Examples:
   - 【低分/不推荐】{{"target": "Traffic", "score": 30, "reason": "【决策依据】近{days}日趋势：ACOS从[某日日期]的[某日ACOS]逐日降至[某日日期]的[某日ACOS]，CVR从[某日CVR]攀升至[某日CVR]，效率正在改善。但{days}日平均CPC $[CPC]仍 > 单均毛利$[毛利]，触碰引流型强制排除红线。{days}日自然单占比[自然单占比]%，曝光已充足。\\n【建议】不推荐在当前阶段开启引流型广告。CPC超毛利意味着引流即亏损。应在转化型巩固已有CVR优势，待CPC降至$[CPC阈值]以下再考虑引流。\\n【后续关注】未来{days}日重点监控CPC走势。若CPC连续3日低于$[CPC阈值]或{days}日自然单占比跌破[阈值]%，需重新评估。"}}
   - 【高分/推荐】{{"target": "Conversion", "score": 85, "reason": "【决策依据】近{days}日趋势：CVR从[某日CVR]攀升至[某日CVR]，呈加速上升；{days}日平均CVR [CVR]%远超基准~[基准]%。{days}日平均ACOS [ACOS]%在推进期容忍上限[上限]%内，且[某日日期]当天ACOS已降至[某日ACOS]%，逐日持续改善。{days}日退款率[退款率]%，转化链路健康。\\n【建议】强烈推荐转化型广告。重点投放长尾精准词（如black string bikini），利用持续走高的CVR收割旺季订单。\\n【后续关注】以{days}日为窗口持续监控CVR和退货率。若{days}日平均CVR跌破[阈值]%或退货率升至[阈值]%以上，需排查listing或竞品动态。"}}. Sort by score descending.

3. For each keyword in the core keywords list, determine its strategy type and put into `keyword_analysis` array. The `action` MUST be in Chinese and differentiate based on the 7d trend AND whether the keyword has a natural ranking:
   - "无排名数据" → 未入榜：有花费的建议提高出价或检查词的相关性；无花费的建议暂不投放，优先推已入榜词
   - "快速上升" → 建议加预算守住排名，防止竞品反超
   - "缓慢上升" → 建议维持当前出价，继续观察
   - "持平" → 建议保持稳定投放即可
   - "轻微下滑" → 建议微调出价或检查竞品动态
   - "快速下滑" → 建议立即排查原因（竞品降价/差评/listing问题），并给出紧急补救措施
   Format: {{"word": "keyword text", "strategy_type": "Broad/Long-tail/Competitor/Brand/Custom", "action": "one-line Chinese ad suggestion based on trend"}}

4. `chart_metrics` array: pick 2-3 metrics most worth monitoring from ["acos", "cvr", "ctr", "cpc", "natural_ratio", "orders", "spend"].
5. NEVER violate the 6 [AI Absolute Red Lines] in the knowledge base!

Please output strictly in JSON format.
"""

    kb_path = os.path.join(os.path.dirname(__file__), "knowledge_base", "prompt_kb.md")
    kw_kb_path = os.path.join(os.path.dirname(__file__), "knowledge_base", "prompt_keyword_kb.md")

    try:
        with open(kb_path, "r", encoding="utf-8") as f:
            system_prompt = f.read()
    except FileNotFoundError:
        return {"error": f"Knowledge base file not found: {kb_path}"}

    try:
        with open(kw_kb_path, "r", encoding="utf-8") as f:
            kw_kb = f.read()
        system_prompt = system_prompt + "\n\n---\n\n" + kw_kb
    except FileNotFoundError:
        pass

    try:
        _t_llm_start = _time.time()
        # 多 Key 轮询 + 失败重试
        _llm_last_exc = None
        response = None
        for _attempt in range(3):
            _api_key = key_pool.next_key()
            try:
                _client = OpenAI(api_key=_api_key, base_url=DEEPSEEK_BASE_URL)
                response = _client.chat.completions.create(
                    model=DEEPSEEK_MODEL,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    temperature=0.1,
                    response_format={"type": "json_object"},
                )
                key_pool.mark_success(_api_key)
                break
            except Exception as _e:
                _err_str = str(_e)
                if "429" in _err_str:
                    key_pool.mark_failed(_api_key, status_code=429)
                elif "401" in _err_str or "403" in _err_str:
                    key_pool.mark_failed(_api_key, status_code=401)
                else:
                    raise
                _llm_last_exc = _e
                continue
        if response is None:
            raise _llm_last_exc or RuntimeError("LLM 调用失败：重试次数耗尽")
        _t_llm_cost = _time.time() - _t_llm_start
        ai_json = json.loads(response.choices[0].message.content)
        ai_targets = ai_json.get("targets", ["Profit"])
        ai_target_scores = ai_json.get("target_scores", [])
        ai_html_reason = ai_json.get("html_report", "Report generation failed.")
        ai_chart_metrics = ai_json.get("chart_metrics", ["acos", "cvr"])
        ai_summary = ai_json.get("summary", "Metrics within normal range.")
        ai_final_action = ai_json.get("final_action", "No special action needed.")
        ai_keyword_analysis = ai_json.get("keyword_analysis", [])
    except Exception as e:
        _t_llm_cost = _time.time() - _t_llm_start if '_t_llm_start' in dir() else 0
        ai_targets = ["Profit"]
        ai_target_scores = []
        ai_html_reason = f"<div style='color:red;'>AI diagnosis error: {e}</div>"
        ai_summary = "Data parsing error"
        ai_final_action = "No action"
        ai_chart_metrics = ["acos", "cvr"]
        ai_keyword_analysis = []

    _nr = (metrics.get('natural_order_ratio') or 0)
    _nc = (metrics.get('net_cvr') or 0)
    _ac = (metrics.get('avg_acos') or 0)
    _as = (metrics.get('avg_star') or 0)
    _anr = (metrics.get('avg_nature_rank') or 0)
    indicators = [
        {"指标": "total_inventory", "当前表现": str(metrics.get("total_inventory", 0))},
        {"指标": f"orders_{days}d", "当前表现": str(orders_total)},
        {"指标": "natural_order_ratio", "当前表现": f"{_nr * 100:.1f}%"},
        {"指标": "net_cvr", "当前表现": f"{_nc * 100:.1f}%"},
        {"指标": "cpc", "当前表现": f"${cpc:.2f}"},
        {"指标": "avg_acos", "当前表现": f"{_ac * 100:.1f}%"},
        {"指标": "avg_star", "当前表现": f"{_as:.2f}"},
        {"指标": "refund_rate", "当前表现": f"{refund_rate_pct:.1f}%"},
        {"指标": "avg_nature_rank", "当前表现": str(round(_anr, 1))},
    ]

    print(f"[TIMING] determine_ad_targets_from_metrics: LLM={_t_llm_cost:.2f}s")
    return {
        "reason": ai_html_reason,
        "summary": ai_summary,
        "final_action": ai_final_action,
        "targets": ai_targets,
        "target_scores": ai_target_scores,
        "indicators": indicators,
        "top_keywords": _keywords,
        "trend_data": _trend,
        "chart_metrics": ai_chart_metrics,
        "keyword_analysis": ai_keyword_analysis,
        "_metrics": metrics,
    }


async def determine_ad_targets(parent_asin: str, shop_account: str, days: int,
                                position: str, stage: str, season: str) -> dict:
    """AI diagnosis function (DB query wrapper). Delegates to determine_ad_targets_from_metrics."""
    _t_db_start = time.time()
    adapter = DbAdapter()
    data = await adapter.fetch_asin_data(parent_asin, days=days)
    _t_db_cost = time.time() - _t_db_start

    if data.data_missing:
        return {"error": f"Data missing: {data.missing_fields}"}

    metrics = asin_data_to_metrics(data, days=days)

    trend_history = [
        {"date": tp.date, "acos": tp.acos, "cvr": tp.cvr,
         "ctr": tp.ctr, "cpc": tp.cpc, "orders": tp.orders, "spend": tp.spend}
        for tp in data.trend
    ]

    exact_words = {kw.keyword for kw in data.keywords if kw.keyword and kw.match_type and kw.match_type.upper() == "EXACT"}
    kw_data_map = {kw.keyword: kw for kw in data.keywords if kw.keyword and kw.match_type and kw.match_type.upper() == "EXACT"}
    kw_top_raw = metrics.get("top_keywords", [])
    seen_words = set()
    kw_top = []
    for kw_entry in kw_top_raw:
        word = kw_entry.get("word", "")
        if word in exact_words and word not in seen_words:
            seen_words.add(word)
            full_kw = kw_data_map.get(word)
            if full_kw:
                kw_entry["sp_rank"] = full_kw.sp_rank or 0
                kw_entry["search_rank"] = full_kw.search_rank or 0
                kw_entry["rank_change_14d"] = full_kw.rank_change_14d or 0
                kw_entry["rank_change_7d"] = full_kw.rank_change_7d or 0
            kw_top.append(kw_entry)

    orders_total = int(metrics.get("total_orders") or 0)
    if orders_total > 0:
        stock_days = int(metrics.get("total_inventory", 0) / (orders_total / float(days)))
    else:
        stock_days = 999 if metrics.get("total_inventory", 0) > 0 else 0

    avg_price = float(metrics.get("avg_price", 0))
    competitor_price = float(data.competitor_price_p50 or avg_price * 0.95)

    result = await determine_ad_targets_from_metrics(
        metrics=metrics,
        position=position, stage=stage, season=season, days=days,
        keywords=kw_top, trend_history=trend_history,
        competitor_price=competitor_price, stock_days=stock_days,
    )
    print(f"[TIMING] determine_ad_targets: DB={_t_db_cost:.2f}s")
    return result

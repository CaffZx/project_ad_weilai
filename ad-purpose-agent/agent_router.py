# agent_router.py
import json
import os
import sys
import time
from pathlib import Path

from config import DEEPSEEK_BASE_URL, DEEPSEEK_MODEL, SIBLING_PROJECT
from openai import APIConnectionError, APIStatusError, APITimeoutError, OpenAI

# Access unified data layer (ad-direction-agent's McpAdapter) — for determine_ad_targets only
_sys_root = Path(__file__).resolve().parent.parent / SIBLING_PROJECT
if str(_sys_root) not in sys.path:
    sys.path.insert(0, str(_sys_root))

from app.data.mcp_adapter import McpAdapter           # noqa: E402
from app.data.field_mapping import asin_data_to_metrics  # noqa: E402
from app.llm.client import get_key_pool             # noqa: E402
from app.models.layers import product_level_with_code  # noqa: E402

_openai_clients: dict[str, OpenAI] = {}


def _get_openai_client(api_key: str) -> OpenAI:
    if api_key not in _openai_clients:
        _openai_clients[api_key] = OpenAI(api_key=api_key, base_url=DEEPSEEK_BASE_URL)
    return _openai_clients[api_key]


_PURPOSE_OUTPUT_RULES = """## 输出要求

你是亚马逊广告精算师。基于上述知识库规则和用户消息中的诊断数据，给出广告目的和关键词类型推荐。

输出 JSON 格式：
- `targets`: level="推荐" 的目标数组。无合格时返回 []。
- `target_scores`: 全部 4 个目标（Traffic/Conversion/Ranking/Profit），各含 level + reason。reason 必须三段：【决策依据】（知识库规则匹配）【建议】【后续关注】。所有目标都必须写完整三段，禁止写"不适用"。
- `level` 判定规则（严格遵循知识库，禁止主观打分）：
  - "推荐": 知识库触发条件命中（04-触发规则.md）且无阻断（guardrail、stage constraint 全通过）
  - "可选": 触发条件未命中但未被阻断，或部分条件满足
  - "不推荐": 被知识库硬护栏（10-安全护栏.md，如CPC>毛利）、产品阶段约束、或触发规则中的阻断条件明确排除
- `keyword_analysis`: 每个关键词的 keyword_class（识别的关键词类别: Broad/Long-tail/Competitor/Brand/Custom）和 action（中文）。
- `chart_metrics`: 从 ["acos", "cvr", "ctr", "cpc", "natural_ratio", "orders", "spend"] 中选 2-3 个最值得关注的。

严禁违反知识库中的安全护栏规则。
"""


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
    _trend_lines: list[str] = []
    for row in trend_history or []:
        if not isinstance(row, dict):
            continue
        _trend_lines.append(
            f"    - {row.get('date')}: orders={row.get('orders')}, ad_orders={row.get('ad_orders')}, "
            f"spend={row.get('spend')}, acos={row.get('acos')}, cvr={row.get('cvr')}, "
            f"ctr={row.get('ctr')}, cpc={row.get('cpc')}"
        )
    _trend = "\n".join(_trend_lines) if _trend_lines else "    (无日趋势数据)"

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
- 产品定位: {product_level_with_code(position) if position else '?'}
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
(⚠️ Due to timezone differences and data capture windows, the LATEST day's data may be incomplete / partially captured. IGNORE the last day's data point when analyzing trends. Base your analysis on the earlier {days-1} days.)
{_trend}

[Core Instructions]
1. `targets` array: ONLY targets whose `level` is "推荐". If none qualify, return [].
2. `target_scores` array: ALL 4 targets (Traffic/Conversion/Ranking/Profit) with their `level` and detailed `reason`. The `reason` field MUST contain three sections using 【】 markers:

   【决策依据】— Match against knowledge base rules using this decision tree, then cite evidence with proper formatting:
         (a) STEP 1 — Trigger check: scan knowledge base trigger conditions (04-触发规则.md). Does any trigger match? Examples: 新品/低样本→Traffic, 订单不足→Conversion, 排名机会→Ranking, 效率稳定→Profit.
         (b) STEP 2 — Guardrail check: scan knowledge base guardrails (10-安全护栏.md) and stage constraints (02-标签维度定义.md). Is this target blocked? Examples: CPC>margin blocks Traffic, CVR<category×30% blocks Traffic, push-stage suppresses Profit, harvest-stage blocks Traffic.
         (c) STEP 3 — Assign level:
             - trigger matched AND no guardrail blocked → level="推荐"
             - trigger NOT matched AND no guardrail blocked → level="可选"
             - guardrail blocked (regardless of trigger) → level="不推荐"
         (d) FORMAT — EVERY metric MUST carry its time window AND caliber (某日当天值 or N日平均值). Write "{days}日平均ACOS [XX]%" not "ACOS [XX]%". For a specific date from trend: "[5月17日]当天ACOS [XX]%". For a trend range: "近{days}日ACOS从[5月12日]的[XX]%逐日降至[5月17日]的[XX]%". For a window average: "{days}日平均CVR [XX]%". Never drop the window or caliber.
         (e) Only cite the 1-2 indicators that truly DETERMINED this level. Ignore secondary factors.

   【建议】— 1-2 sentences. "推荐"→what to prioritize; "可选"→when this becomes viable; "不推荐"→what must change first.

   【后续关注】— 1-2 conditions that would change this level on re-evaluation.

   Examples:
   - 【不推荐】{{"target": "Traffic", "level": "不推荐", "reason": "【决策依据】STEP1触发检查：产品阶段=推进期，引流型未被阶段约束阻断。STEP2护栏检查：{days}日平均CPC $[CPC] > 单均毛利$[毛利]，触碰知识库 CPC>毛利 硬阻断规则。STEP3结论：level=不推荐。\\n【建议】当前不应开启引流型广告，每次引流都在亏损。优先通过转化型广告巩固CVR优势，待CPC降至$[毛利]以下再重新评估。\\n【后续关注】每日监控CPC与毛利差值。若CPC连续3日低于$[毛利]，重新评估引流型level。"}}
   - 【可选】{{"target": "Ranking", "level": "可选", "reason": "【决策依据】STEP1触发检查：{days}日自然排名第[XX]位，7日排名上升[XX]位，排名机会触发条件部分满足。STEP2护栏检查：{days}日平均ACOS [XX]%在推进期容忍上限[XX]%内，未被阻断。但7日预算利用率仅[XX]%，未达60%门槛，不满足完整触发条件。STEP3结论：level=可选。\\n【建议】排名上升趋势存在但预算利用率不足。可小幅加投测试排名反应，若预算利用率升至60%以上且ACOS不恶化，level可升至推荐。\\n【后续关注】每日监控预算利用率和ACOS联动变化。"}}

3. For each keyword in the core keywords list, determine its strategy type and put into `keyword_analysis` array. The `action` MUST be in Chinese and differentiate based on the 7d trend AND whether the keyword has a natural ranking:
   - "无排名数据" → 未入榜：有花费的建议提高出价或检查词的相关性；无花费的建议暂不投放，优先推已入榜词
   - "快速上升" → 建议加预算守住排名，防止竞品反超
   - "缓慢上升" → 建议维持当前出价，继续观察
   - "持平" → 建议保持稳定投放即可
   - "轻微下滑" → 建议微调出价或检查竞品动态
   - "快速下滑" → 建议立即排查原因（竞品降价/差评/listing问题），并给出紧急补救措施
   Format: {{"word": "keyword text", "keyword_class": "Broad/Long-tail/Competitor/Brand/Custom", "action": "one-line Chinese ad suggestion based on trend"}}

4. `chart_metrics` array: pick 2-3 metrics most worth monitoring from ["acos", "cvr", "ctr", "cpc", "natural_ratio", "orders", "spend"].
5. NEVER violate the 6 [AI Absolute Red Lines] in the knowledge base!

Please output strictly in JSON format.
"""

    from app.llm.kb_loader import kb           # noqa: E402

    # 替换旧 KB 为 docs/knowledge_base 切片
    system_prompt = kb.build("purpose_tactics") + "\n\n---\n\n" + _PURPOSE_OUTPUT_RULES

    try:
        _t_llm_start = _time.time()
        key_pool = get_key_pool()
        if key_pool is None:
            raise RuntimeError("LLM API 密钥未配置")
        # 多 Key 轮询 + 失败重试
        _llm_last_exc = None
        response = None
        for _attempt in range(3):
            _api_key = key_pool.next_key()
            try:
                _client = _get_openai_client(_api_key)
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
            except APIStatusError as _e:
                sc = _e.status_code
                if sc == 429:
                    key_pool.mark_failed(_api_key, status_code=429)
                elif sc in (401, 403):
                    key_pool.mark_failed(_api_key, status_code=401)
                else:
                    key_pool.mark_failed(_api_key, status_code=500)
                _llm_last_exc = _e
                continue
            except (APIConnectionError, APITimeoutError) as _e:
                key_pool.mark_failed(_api_key, status_code=500)
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
        ai_targets = []
        _fb = "【决策依据】AI 评分服务暂不可用。\\n【建议】请点击右下方「AI 重新推荐」重试。\\n【后续关注】服务恢复后重新评估。"
        ai_target_scores = [
            {"target": "Traffic", "level": "不推荐", "reason": _fb},
            {"target": "Conversion", "level": "不推荐", "reason": _fb},
            {"target": "Ranking", "level": "不推荐", "reason": _fb},
            {"target": "Profit", "level": "不推荐", "reason": _fb},
        ]
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
    adapter = McpAdapter()
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

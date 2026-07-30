"""临时脚本：抓取新增词 LLM 的原始注入 prompt 和原始输出。

用法：启动 uvicorn 前加环境变量即可生效，无需改代码：

    set DUMP_NEW_CAMPAIGN_LLM=1 && uvicorn app.main:app ...

或直接 import：

    python -c "import scripts.dump_new_campaign_llm; ..."

输出位置：logs/new_campaign_dump/{asin}_{timestamp}_round{N}_batch{M}.txt (prompt)
          logs/new_campaign_dump/{asin}_{timestamp}_round{N}_batch{M}_response.txt (raw output)
"""

import asyncio
import json as _json
import os as _os
import time as _time
from pathlib import Path as _Path

_ORIGINAL = None
_OUT_DIR = _Path(__file__).resolve().parent.parent / "logs" / "new_campaign_dump"


def _install() -> None:
    global _ORIGINAL
    from app.llm.reasoner import LLMReasoner

    _ORIGINAL = LLMReasoner.recommend_new_campaigns

    async def _patched(
        self,
        asin: str,
        candidates: list[dict],
        strategy_context: dict,
        temperature: float = 0.3,
        timeout_override: float | None = None,
        *,
        product_title: str = "",
        existing_keywords: list[str] | None = None,
    ) -> dict:
        # 用调用序号区分同一 asin 的多轮/多批
        _patched._call_count += 1  # type: ignore[attr-defined]
        call_seq = _patched._call_count  # type: ignore[attr-defined]
        round_label = getattr(_patched, "_round_label", "?")

        # 调原方法
        result = await _ORIGINAL(
            self, asin, candidates, strategy_context, temperature,
            timeout_override, product_title=product_title,
            existing_keywords=existing_keywords,
        )

        # 写文件
        _OUT_DIR.mkdir(parents=True, exist_ok=True)
        ts = _time.strftime("%Y%m%d_%H%M%S")
        safe_asin = asin.replace("/", "-").replace("\\", "-").replace(":", "-")
        prefix = f"{safe_asin}_{ts}_seq{call_seq:02d}_n{len(candidates)}"

        # 重建 prompt（与 reasoner.py:1955-1963 同逻辑）
        from app.llm.reasoner import _build_new_campaign_prompt

        ctx_parts = [
            "## 策略上下文 (ASIN 级，全批共享)",
            # [产品阶段] 已由经营模式替代
            # f"  - 产品阶段: {strategy_context.get('product_stage', '?')}",
            f"  - 产品定位: {strategy_context.get('product_level', '?') or '?'}",
            f"  - 淡旺季: {strategy_context.get('season_stage', '?')}",
            f"  - 广告目的: {strategy_context.get('ad_purposes', [])}",
            f"  - 目标关键词类型: {strategy_context.get('target_keyword_strategy', [])}",
        ]
        if strategy_context.get("target_acos"):
            ctx_parts.append(f"  - 运营目标 ACOS: {strategy_context['target_acos']}%")
        _adirs = strategy_context.get("ad_directions") or []
        if _adirs:
            ctx_parts.append(f"  - 广告方向(运营已选): {_adirs}")

        anchor_parts = ["## 本产品标题 + 已投放关键词（相关性参照）"]
        if (product_title or "").strip():
            anchor_parts.append(f"  - 产品标题: {product_title.strip()[:300]}")
        _exist = [k for k in (existing_keywords or []) if str(k).strip()][:60]
        if _exist:
            anchor_parts.append(f"  - 已投放关键词({len(_exist)}个，相关性参照): {', '.join(_exist)}")
        else:
            anchor_parts.append("  - 已投放关键词: 无（新品/小 ASIN，锚点稀薄 → 以标题为主判相关性，勿过度 skip）")

        overview_text = (strategy_context.get("_strategic_overview_text") or "").strip()
        preamble = (
            f"## 今日执行总纲（逐词判断须遵循此宏观框架）\n{overview_text}\n\n---\n\n"
            if overview_text else ""
        )

        cand_parts = ["## 候选关键词列表 (逐词判断 action + keyword_class + relevance_tier)"]
        for i, c in enumerate(candidates):
            rank = c.get("natural_rank")
            trend = c.get("rank_trend") or ""
            sr = c.get("search_rank")
            wk = c.get("week_rank")
            wsv = c.get("week_search_volume")
            tier = c.get("rank_tier") or ""
            sp = c.get("sponsored_rank")
            his = c.get("history_state") or ""
            line = (
                f"\n### 候选 {i + 1}: {c.get('keyword_text', '')}"
                f"\n  - 搜索量: {c.get('search_volume', 0)}"
                + (f"\n  - 搜索排名: {sr}" if sr is not None else "")
                + (f"\n  - 周搜索量: {wsv}" if wsv is not None else "")
                + f"\n  - 当前自然位: {rank if rank is not None else 'N/A(无自然位)'}"
                + (f"\n  - 自然位趋势(7天): {trend}" if trend else "")
                + (f"\n  - 自然位分位: {tier}" if tier else "")
                + (f"\n  - 广告排位: {sp}" if sp is not None else "")
                + (f"\n  - 词的周排名: {wk}" if wk is not None else "")
                + (f"\n  - 历史数据: {his}" if his and his != "ok" else "")
                + f"\n  - 触发场景(参考): {c.get('trigger_scene', '')}"
            )
            if c.get("source"):
                line += f"\n  - 来源: {c.get('source')}"
            if c.get("source_reason"):
                line += f"\n  - 来源说明: {c.get('source_reason')}"
            cand_parts.append(line)

        user_message = (
            preamble + "\n".join(ctx_parts) + "\n" + "\n".join(anchor_parts)
            + "\n" + "\n".join(cand_parts)
        )
        system_prompt = _build_new_campaign_prompt()

        # 写 prompt
        prompt_text = (
            f"=== SYSTEM ===\n{system_prompt}\n\n=== USER ===\n{user_message}"
        )
        prompt_file = _OUT_DIR / f"{prefix}_prompt.txt"
        prompt_file.write_text(prompt_text, encoding="utf-8")

        # 写 raw response
        raw_output = result.get("raw_output", "")
        resp_file = _OUT_DIR / f"{prefix}_response.txt"
        resp_file.write_text(str(raw_output), encoding="utf-8")

        # 写 parsed summary
        parsed = result.get("parsed", {})
        new_count = len(parsed.get("new_campaigns", []) or [])
        summary_file = _OUT_DIR / f"{prefix}_summary.txt"
        summary_file.write_text(
            f"success={result.get('success')}\n"
            f"error={result.get('error')}\n"
            f"new_campaigns_count={new_count}\n"
            f"parsed keys={list(parsed.keys()) if isinstance(parsed, dict) else 'N/A'}\n",
            encoding="utf-8",
        )

        return result

    _patched._call_count = 0  # type: ignore[attr-defined]
    LLMReasoner.recommend_new_campaigns = _patched
    print(f"[dump_new_campaign_llm] 已安装 monkey-patch → {_OUT_DIR}")


# ── 入口：环境变量控制 ──
if _os.environ.get("DUMP_NEW_CAMPAIGN_LLM", "").strip() in ("1", "true", "yes"):
    _install()
else:
    print("[dump_new_campaign_llm] 未设置 DUMP_NEW_CAMPAIGN_LLM=1，跳过安装。"
          " 如需启用请 set DUMP_NEW_CAMPAIGN_LLM=1")

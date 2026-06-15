#!/usr/bin/env python3
"""Capture synthesis LLM raw input/output for B0B7S3PWWB debugging.

Run: cd ad-direction-agent && python ../scripts/debug_synthesis.py B0B7S3PWWB
Saves: scripts/synthesis_input.json, scripts/synthesis_output.txt
"""

import asyncio, json, os, sys, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "ad-direction-agent"
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(str(ROOT / ".env"))

from app.llm.reasoner import reasoner, _build_campaign_synthesis_prompt
from app.llm.client import deepseek_client
from app.config.settings import settings

async def main(asin: str, days: int = 7):
    # 1. 跑一次真实分析拿到 adjustments
    from app.api.campaign import _do_analyze

    print(f"[1] 运行 campaign 分析 [{asin}] days={days} ...")
    t0 = time.monotonic()
    result, extra = await _do_analyze({"asin": asin, "days": days, "temperature": 0.3})
    print(f"[1] 完成 {time.monotonic()-t0:.0f}s, adjustments={len(result.adjustments)}")

    adjustments = result.adjustments
    strategy_context = extra["strategy_ctx"].model_dump() if extra and extra.get("strategy_ctx") else {}

    # 2. 构建 synthesis 输入（与 recommend_campaign_synthesis 同逻辑）
    compact = []
    for adj in adjustments:
        d = adj.model_dump()
        compact.append({
            "campaign_key": d.get("campaign_key", ""),
            "campaign_name": (d.get("campaign_name") or "")[:60],
            "action": d.get("action", ""),
            "triggered_rule": d.get("triggered_rule", ""),
            "match_type": d.get("match_type", ""),
            "reason": d.get("reason") or "",
        })

    ctx_lines = [
        f"  - 产品阶段: {strategy_context.get('product_stage', '?')}",
        f"  - 广告目的: {strategy_context.get('ad_purposes', [])}",
        f"  - 目标 ACOS: {strategy_context.get('target_acos', '?')}%",
    ]
    user_message = (
        "## 策略上下文\n"
        + "\n".join(ctx_lines)
        + f"\n\n## 待合成的 {len(compact)} 条单活动建议（JSON 数组）\n"
        + json.dumps(compact, ensure_ascii=False)
    )
    messages = [
        {"role": "system", "content": _build_campaign_synthesis_prompt()},
        {"role": "user", "content": user_message},
    ]

    # 保存输入
    in_path = Path(__file__).resolve().parent / "synthesis_input.json"
    in_path.write_text(json.dumps({
        "system_prompt": _build_campaign_synthesis_prompt(),
        "user_message": user_message,
        "compact_count": len(compact),
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[2] 输入已保存: {in_path} ({len(in_path.read_text(encoding='utf-8'))} chars)")

    # 3. 调 LLM — 不传 max_tokens，让模型自己定；不计超时
    print(f"[3] 调用 synthesis LLM (model={settings.llm_model_strong}, thinking=True, no max_tokens) ...")
    t0 = time.monotonic()
    raw = await deepseek_client.chat(
        messages=messages,
        temperature=0.3,
        response_format={"type": "json_object"},
        max_tokens=None,  # 不限，让模型说了算
        model=settings.llm_model_strong,
        thinking=True,
        reasoning_effort=settings.llm_strong_reasoning_effort,
    )
    elapsed = time.monotonic() - t0

    out_path = Path(__file__).resolve().parent / "synthesis_output.txt"
    out_path.write_text(raw, encoding="utf-8")
    print(f"[3] 完成 {elapsed:.0f}s, raw_len={len(raw)} chars, saved: {out_path}")
    print(f"     raw[:200] = {raw[:200]}")

    # 4. 尝试解析
    try:
        parsed = reasoner._parse_json(raw)
        groups = len(parsed.get("groups") or [])
        specials = len(parsed.get("special_cases") or [])
        print(f"[4] 解析成功: groups={groups} specials={specials}")
    except Exception as e:
        print(f"[4] 解析失败: {e}")

if __name__ == "__main__":
    asin = sys.argv[1] if len(sys.argv) > 1 else "B0B7S3PWWB"
    days = int(sys.argv[2]) if len(sys.argv) > 2 else 7
    asyncio.run(main(asin, days))

# Campaign 分析引擎：切除双轮投票 + 护栏重试统一方案

> **版本**: v4.0  
> **日期**: 2026-07-31  
> **状态**: 待实施  

---

## 1. 目标

1. 分批大小从 6 改为 10
2. 切除流内 R1/R2 并行双轮投票，统一为单轮 R1 + 护栏驱动的 R2/R3/R4 重试
3. 护栏告警跨轮累积传递
4. 保留既有缺失补答行为（旧 R3 tiebreaker → 新 R2/R3/R4 补答循环）

---

## 2. 当前架构

```
_analyze_one_stream() ×2（exact/broad 并行）
  ├─ < batch_size*2 → 单轮 R1（默认 6 时 <12）
  └─ ≥ batch_size*2:
       ├─ R1+R2 并行 (种子1,2)
       ├─ _vote() → 一致=high, 分歧=low, 单边缺失=low
       ├─ R3 tiebreaker (分歧项 + 缺失项补答)
       └─ _merge_to_adjustments()

↓ 合流 (L695-696)
   skipped_campaigns = exact_skipped + broad_skipped + skipped_eliminated + excluded

护栏重试 for (3,"R3"), (4,"R4"):     # L784
  ├─ _apply_campaign_guardrails()
  ├─ alerts = _build_guardrail_alerts(...)   # 仅含 retry_instruction 非空的项
  ├─ failed_keys = set(alerts)               # 每轮覆盖
  ├─ 为空 → break
  ├─ _inject_alerts_to_summaries(alerts)     # s["_guardrail_alert"] = alerts[ckey]
  ├─ _run_round(仅 failed_keys, seed=round_number)
  └─ replacement_by_key → adjustments[idx] = replacement
else:                                    # 仅循环未 break 时执行
  └─ 最终护栏强制修正

llm_rounds_completed=2  # 硬编码 L1094
```

**既有缺失补答**：`_vote()` 标记单边缺失/双边缺失为 `needs_tiebreaker` → R3 tiebreaker 补答。

**告警覆盖根因**：`alerts` 变量每轮覆盖 + `_inject_alerts_to_summaries` 用 `=` 赋值。

---

## 3. 目标架构

```
_analyze_one_stream() ×2（保留并行）
  └─ 统一单轮 R1 (种子1)

↓ 合流

llm_skipped = exact_skipped + broad_skipped  # 仅 LLM 真正丢失的
unresolved_keys = {llm_skipped 的 campaign_key}

护栏+补答重试 for (2,"R2"), (3,"R3"), (4,"R4"):
  ├─ _apply_campaign_guardrails()
  ├─ 告警累积: guardrail_alert_history 逐条 retry_instruction 去重
  ├─ guardrail_failed = set(_build_guardrail_alerts(...))  # 仅 retry_instruction 非空
  ├─ retry_keys = guardrail_failed | unresolved_keys
  ├─ 为空 → break
  ├─ 构建 retry_source（来自 exact_summaries + broad_summaries）
  ├─ 循环内按 exact/broad 分别注入 cumulative
  ├─ _run_round(仅 retry_keys, seed=round_number)
  ├─ 结果为空 → continue（统一带入下一轮，3 轮上限）
  ├─ 护栏失败且返回 → replacement
  ├─ 缺失补答成功:
  │   ├─ 已在 adjustments → replacement（防重复）
  │   └─ 不在 adjustments → append + backfill + 精准规则 + 从 skipped 移除
  └─ unresolved_keys = retry_keys - returned_keys
else:
  └─ 最终护栏强制修正（行为不变）

llm_rounds_completed = 实际最高轮次 (1-4)
```

---

## 4. 代码改动

### 4.1 `settings.py:123` — 分批大小

```python
campaign_batch_size: int = 10  # 原 6
```

### 4.2 `campaign.py` `_analyze_one_stream()` — 切除双轮投票

**删除** L1575-1625（`< batch_size*2` 分支 + R1+R2 gather + `_vote()` + R3 tiebreaker）。

**替换为**：

```python
    # 3. 分批 + R1 单轮分析
    batches = _build_batches(summaries, batch_size, seed=1)
    r1_results = await _run_round(
        reasoner, parent_asin, batches, ctx_dict, temperature, sem, 1,
        task_type=task_type, cancel_check=cancel_check,
    )
    _st(f"DONE R1 ({len(batches)} batches)")
    await _cancel()

    adjustments: list[CampaignAdjustmentItem] = []
    for br in r1_results:
        for item in br.items:
            item.confidence = "medium"
            adjustments.append(item)
    skipped = _collect_skipped(campaigns, adjustments)
    if task_type == "exact":
        _backfill_placement_pcts(adjustments, unit_lookup)
    return adjustments, {
        "round1": _round_stats(r1_results), "round2": None, "round3": None,
    }, summaries, skipped
```

**不删除死代码**：`_vote`/`_conservative`/`_resolve_tiebreaker`/`_vote_key`/`_item_to_vote`/`_placement_sig`/`_merge_negative_keywords` 保留，后续单独清理。

### 4.3 `campaign.py` 护栏循环 — 3 轮 + 告警累积 + 缺失补答

#### 4.3.1 轮次定义 (L784)

```python
retry_rounds = ((2, "R2"), (3, "R3"), (4, "R4"))
```

#### 4.3.2 循环前初始化（L784 后插入）

```python
    guardrail_alert_history: dict[str, list[str]] = {}

    # 仅 LLM 真实丢失的活动（exact_skipped/broad_skipped 来自 _collect_skipped，
    # 输入已是预过滤后的 LLM 分析集，天然不含 __prefiltered/excluded）
    unresolved_keys: set[str] = {
        s.get("campaign_key", "")
        for s in (exact_skipped + broad_skipped)
        if s.get("campaign_key")
    }
```

#### 4.3.3 循环体（替换 L786-916）

```python
    for round_number, round_label in retry_rounds:
        guardrail_pass, budget_warnings = _apply_campaign_guardrails(
            adjustments,
            product_stage=strategy_context.product_stage,
            inventory_days=strategy_context.inventory_days,
            refund_rate=strategy_context.refund_rate,
            rating=strategy_context.rating,
        )
        warnings_list.extend(budget_warnings)

        # 告警累积（逐条 retry_instruction 去重）
        for r in guardrail_pass.results:
            if not r.corrected:
                continue
            key = getattr(r, "campaign_key", "") or ""
            instruction = (getattr(r, "retry_instruction", "") or "").strip()
            if not key or not instruction:
                continue
            entries = guardrail_alert_history.setdefault(key, [])
            if instruction not in entries:
                entries.append(instruction)
        cumulative = {k: "\n".join(v) for k, v in guardrail_alert_history.items()}

        # 护栏失败项（仅 retry_instruction 非空，与现有行为一致）
        alerts = _build_guardrail_alerts(guardrail_pass)
        guardrail_failed = set(alerts)

        # 合并入口：护栏失败 ∪ 待补答
        retry_keys = guardrail_failed | unresolved_keys
        if not retry_keys:
            logger.info(
                "Guardrail %s pass [%s]: corrections=%d rules=%s",
                round_label, parent_asin, guardrail_pass.corrections,
                _guardrail_rule_counts(guardrail_pass),
            )
            break

        logger.info(
            "Guardrail %s blocked [%s]: corrections=%d failed=%d unresolved=%d retry=%d/%d rules=%s",
            round_label, parent_asin, guardrail_pass.corrections,
            len(guardrail_failed), len(unresolved_keys),
            len(retry_keys), len(adjustments) + len(unresolved_keys),
            _guardrail_rule_counts(guardrail_pass),
        )

        # 构建重试源
        retry_source = []
        for s in (exact_summaries + broad_summaries):
            if s.get("campaign_key") in retry_keys:
                retry_source.append(dict(s))
        if not retry_source:
            logger.warning(
                "Guardrail %s retry skipped [%s]: no source for retry_keys=%s",
                round_label, parent_asin, sorted(retry_keys),
            )
            if unresolved_keys:
                continue  # 缺失项仍需补答，带到下一轮
            break

        exact_retry = [s for s in retry_source if (s.get("match_type") or "").upper() == "EXACT"]
        broad_retry = [s for s in retry_source if (s.get("match_type") or "").upper() != "EXACT"]

        alert_text = _GUARDRAIL_RETRY_INSTRUCTION
        retry_items: list[CampaignAdjustmentItem] = []

        for retry_summaries, task_type_name, retry_sem in (
            (exact_retry, "exact", exact_sem),
            (broad_retry, "broad", broad_sem),
        ):
            if not retry_summaries:
                continue
            # 在循环内注入：每种匹配类型分别注入累计告警
            _inject_alerts_to_summaries(retry_summaries, cumulative)
            ctx_dict["_guardrail_instruction"] = alert_text
            try:
                results = await _run_round(
                    reasoner, parent_asin,
                    _build_batches(retry_summaries, bs, seed=round_number),
                    ctx_dict, temperature, retry_sem, round_number,
                    task_type=task_type_name,
                    cancel_check=cancel_check,
                )
                for br in results:
                    retry_items.extend(br.items)
                llm_rounds_completed = max(llm_rounds_completed, round_number)
                logger.info(
                    "Guardrail %s retry result [%s|%s]: batches=%s items=%d",
                    round_label, parent_asin, task_type_name,
                    _round_stats(results), len(retry_items),
                )
            except AnalysisRunCancelled:
                raise
            except Exception as e:
                logger.warning(
                    "Guardrail %s retry failed [%s|%s]: %s",
                    round_label, parent_asin, task_type_name, e,
                )
            finally:
                ctx_dict.pop("_guardrail_instruction", None)

        returned_keys = {item.campaign_key for item in retry_items}

        if not retry_items:
            logger.warning(
                "Guardrail %s empty result [%s]: %d retry_keys, carrying to next round",
                round_label, parent_asin, len(retry_keys),
            )
            continue

        # ── 处理返回结果：统一 upsert ──
        # 已在 adjustments → 替换；不在 → 追加。不区分护栏失败/缺失补答。

        newly_appended: list[CampaignAdjustmentItem] = []
        for ri in retry_items:
            key = ri.campaign_key
            ri.confidence = "medium"
            replaced = False
            for idx, item in enumerate(adjustments):
                if item.campaign_key == key:
                    adjustments[idx] = ri
                    replaced = True
                    break
            if not replaced:
                adjustments.append(ri)
                newly_appended.append(ri)

        # 回填 + 从 skipped 移除
        recovered_keys = {item.campaign_key for item in newly_appended}
        if recovered_keys:
            skipped_campaigns[:] = [
                s for s in skipped_campaigns
                if s.get("campaign_key") not in recovered_keys
            ]
            _backfill_campaign_adjustment_context(
                adjustments, unit_by_key, keyword_class_map, _rank_evidence_line,
                core_keyword_set=core_keyword_set,
            )
            _backfill_placement_pcts(adjustments, unit_by_key)
            for item in adjustments:
                cid = (item.campaign_id or "").strip()
                item.days_since_reactivation = recent_reactivated.get(cid, -1)
            logger.info(
                "Guardrail %s recovered [%s]: %d campaigns, skipped now=%d",
                round_label, parent_asin, len(recovered_keys), len(skipped_campaigns),
            )

        # ③ 对本轮补回的 EXACT 活动执行精准确定性规则
        if newly_appended and getattr(settings, "exact_transition_enabled", False):
            newly_exact = [a for a in newly_appended if (a.match_type or "").upper() == "EXACT"]
            if newly_exact:
                _apply_exact_transition_rules(
                    newly_exact, unit_by_key, strategy_context,
                    campaign_data, core_keyword_set,
                )

        # ④ 下轮待补答：本轮送进去但没吐出来的，一律带入下一轮
        unresolved_keys = retry_keys - returned_keys
        if unresolved_keys:
            logger.warning(
                "Guardrail %s still unresolved [%s]: %s",
                round_label, parent_asin, sorted(unresolved_keys),
            )
    else:
        # 最终护栏强制修正（循环正常耗尽时执行。注意：continue 不阻止 else，仅 break 会跳过）
        guardrail_pass, budget_warnings = _apply_campaign_guardrails(
            adjustments,
            product_stage=strategy_context.product_stage,
            inventory_days=strategy_context.inventory_days,
            refund_rate=strategy_context.refund_rate,
            rating=strategy_context.rating,
        )
        warnings_list.extend(budget_warnings)
        logger.warning(
            "Guardrail final fallback after R4 [%s]: corrections=%d rules=%s",
            parent_asin, guardrail_pass.corrections,
            _guardrail_rule_counts(guardrail_pass),
        )
```

#### 4.3.5 循环后重排序

补答 append 的活动排在末尾，需在护栏循环结束后重新执行 action 排序（与 L737-738 一致）：

```python
    # 护栏循环结束后，重排序（补答活动可能打乱原有顺序）
    action_order = {"eliminate_to_low_bid_pool": 0, "adjust_bid": 1, "adjust_budget": 1, "adjust_placement": 1, "keep": 2}
    adjustments.sort(key=lambda x: action_order.get(x.action, 9))
```

#### 4.3.6 批次大小参数统一（原 L842）

```python
# 改前
_build_batches(retry_summaries, settings.campaign_batch_size, seed=round_number)

# 改后
_build_batches(retry_summaries, bs, seed=round_number)
```

### 4.4 `llm_rounds_completed` 动态化

#### 初始化（L608 附近）

```python
    llm_rounds_completed = 1
```

#### 循环内更新

已在 4.3.3 的 `_run_round` 成功后 `llm_rounds_completed = max(llm_rounds_completed, round_number)`。

#### 返回值 (L1094)

```python
        llm_rounds_completed=llm_rounds_completed,  # 原: 硬编码 2
```

### 4.5 注释同步

| 文件 | 位置 | 改前 | 改后 |
|------|------|------|------|
| `campaign.py` | `_analyze_one_stream` docstring (L1533) | "R1+R2 → 投票 → (R3)" | "R1 单轮" |
| `campaign.py` | L~740 注释 | "R1+R2 投票后" | "R1 分析后" |
| `app/persistence/erp_writer/mappers.py` | L598 | "两轮+R3 均未返回" | "R1+补答循环均未返回" |
| `app/llm/reasoner.py` | — | "丢失活动送 R3 复核" | "丢失活动进入补答循环" |

---

## 5. 关键设计决策与边界行为

### 5.1 unresolved_keys 生命周期

```text
初始化: exact_skipped + broad_skipped 的 campaign_key（纯 LLM 丢失项）

每轮结束: unresolved_keys = retry_keys - returned_keys
  ↑ 不管哪轮缺失，送进去没吐出来就带入下一轮。R4 后仍 unresolved → 留在 skipped_campaigns
```

### 5.2 空结果

`retry_items` 为空时统一 `continue`，不区分护栏失败/缺失补答。3 轮上限防死循环，最终 `else` 兜底。

### 5.3 结果处理：统一 upsert

返回的 key 已在 `adjustments` → 替换；不在 → 追加。不区分护栏失败/缺失补答，不维护 `existing_keys` 中间集合。

### 5.4 guardrail_failed 范围

仅 `_build_guardrail_alerts()` 有值的项（`corrected=True` 且 `retry_instruction` 非空），与现有行为一致。`corrected=True` 但无指导文案的不送 LLM 重试。

### 5.5 置信度

所有调整项统一 `confidence="medium"`。旧 `high`/`low` 不再产生。sanity check（依赖 `confidence=="low"` 筛选）本次暂不调整，留待后续单独处理。

### 5.6 精准规则覆盖补回

补回的 EXACT 活动在 backfill 后立即调用 `_apply_exact_transition_rules()`，不依赖后续循环。

### 5.7 不变的部分

| 项 | 说明 |
|----|------|
| 精准流/广泛流并行 | `asyncio.gather` 保留 |
| `for...else` 最终护栏 | 行为不变 |
| `_GUARDRAIL_RETRY_INSTRUCTION` | 文案不动 |
| `campaign_guardrails.py` | P0-P11 零改动 |
| `campaign_new.py` | 双轮候选词交集不动 |
| 数据库表结构 | `round_votes` 列保留不填充 |
| 死代码 | 8 个投票函数保留，后续单独清理 |

---

## 6. 测试

### 6.1 单元测试

| 测试 | 内容 |
|------|------|
| `test_campaign_guardrail_retry.py` | 更新轮次标签；新增跨轮告警累积断言；新增缺失补答用例 |

### 6.2 集成测试

| # | 场景 | 验证点 |
|----|------|------|
| 1 | B0B7S3PWWB 全链路 | 调整项数量、action 分布 |
| 2 | 活动数 5 / 10 / 25 | 均走单轮 R1，无投票 |
| 3 | 活动数 20 | 旧代码 batch_size=6 时 ≥12 走双轮；新代码仅 R1。**关键回归点** |
| 4 | R1 后零护栏 + 零缺失 | R2/R3/R4 跳过 |
| 5 | `__prefiltered=True` 的活动 | 绝不进入 retry_keys |
| 6 | R1 缺失、R2 整批无返回 | 仍进入 R3、R4（不 break） |
| 7 | R1 缺失、R2 补答成功 | adjustments 追加、skipped 移除、EXACT 经过精准规则 |
| 8 | 护栏失败项 retry 未返回 → 下轮返回 | replacement，不重复 append |
| 9 | 某活动连续触发护栏 3 轮 | R4 后最终兜底修正 |
| 10 | R1-R4 全部缺失 | 最终留在 skipped_campaigns |

---

## 7. 部署

1. `.env` 或环境变量 `CAMPAIGN_BATCH_SIZE` 会覆盖代码默认值。部署前确认服务器无旧值（6）
2. LLM 调用量：首次分析从 2 轮减为 1 轮，护栏重试从 2 轮增为 3 轮（仅子集 + 缺失项），总量预期持平
3. `llm_rounds_completed` 值从固定 2 变为动态 1-4，`auto_push.py:72` 读取 `data.get("llm_rounds_completed") or 0` 兼容
4. 回滚：按完整提交回滚

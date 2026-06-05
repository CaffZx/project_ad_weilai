# ERP 广告方向枚举 — Agent 与 WHP 统一码表

> **适用字段**  
> - `t_advert_agent_direction_recommend_detail.direction_type`  
> - `t_advert_agent_decision.advert_direction_types`（JSON 数组字符串）  
> **实现**：`app/persistence/erp_writer/text_utils.py` → `map_direction_type` / `map_direction_types_json`

---

## 1.  canonical 枚举（写入 ERP 的唯一值）

| ERP 码 | 中文 | Agent 内部 id |
|--------|------|----------------|
| `PUSH_NATURAL` | 推进自然位 | `push_natural` |
| `EXPAND_KEYWORDS` | 新增扩词 | `expand_keywords` |
| `OPTIMIZE_ACOS` | 优化 ACOS | `optimize_acos` |
| `BALANCE_MAINTAIN` | 平衡维持 | `balance_maintain` |

---

## 2. 遗留码（入库时自动映射为 canonical）

| 遗留码（勿再写入） | 映射为 |
|-------------------|--------|
| `PROMOTE_NATURAL_RANK` | `PUSH_NATURAL` |
| `ADD_KEYWORD_EXPANSION` | `EXPAND_KEYWORDS` |
| `BALANCE_MAINTENANCE` | `BALANCE_MAINTAIN` |
| `OPTIMIZE_ACOS` | `OPTIMIZE_ACOS`（不变） |

中文标签「推进自然位」「新增扩词」等亦会映射到上表 ERP 码。

---

## 3. `recommend_tag`（方向卡状态）

| ERP 值 | 建议中文展示 |
|--------|----------------|
| `not_recommended` | 暂不建议 |
| `available` | 可考虑 |
| `recommended` | 推荐 |

`content_json` 为 `{"reason":"…"}`；状态/分数用列 `recommend_tag`、`suggest_score`。

---

## 4. DDL 注释与实测差异（给 DBA/WHP）

| 项目 | DDL/旧文档 | Agent 实测 |
|------|------------|------------|
| `direction_type` 注释 | `PROMOTE_NATURAL_RANK` / `ADD_KEYWORD_EXPANSION` / … | `PUSH_NATURAL` / `EXPAND_KEYWORDS` / … |
| `content_json` 注释 | JSON 字符串**数组** | JSON 对象，**仅 `reason` 键** |
| `recommend_tag` 注释 | 中文文案 | 英文码（`not_recommended` / `available` / `recommended`） |

**结论**：以本文 canonical 码为准；DDL 注释建议后续修订与 WHP 枚举表对齐。

---

## 5. `campaign_group_type`（分析建议卡组合）

| ERP 码 (`campaignGroupType`) | 中文（Agent `ai_portfolio_class`） |
|----------------------------|----------------------------------|
| `exact_core_group` | 精准主力组 |
| `exact_testing_group` | 精准测试组 |
| `auto_broad_group` | 自动广泛组 |
| `low_bid_retention_group` | 低价捡漏组 |

遗留中文（主推 / 测试/新增 / 广泛/自动 / 淘汰）与遗留码（`core` / `test` / `auto_broad` / `eliminate`）入库时自动映射为上表。

实现：`map_campaign_group_type()` in `app/persistence/erp_writer/text_utils.py`。

---

## 6. 校验

```bash
python scripts/erp_db/audit_latest_writes.py
```

`VALID_DIRECTION` 与上表 §1 一致。

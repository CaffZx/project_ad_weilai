# WHP 对接说明：Tab4 渲染问题与 Campaign 组合枚举

> **版本**：2026-06-04  
> **适用**：WHP 前端 + Agent ERP 写入（v3.0.2）  
> **测试库**：`192.168.2.51:3306` / `erp_agentadvert`  
> **校验样本**：ASIN `B0CGH9QRKK`，decision `dec8fafa8afb8a3b58b67a8d3c4bf3da`

本文档合并说明两件事：**Tab4 方向卡为何出现整段 JSON**（如何改前端），以及 **Campaign 分析建议里组合类型的中英文/ERP 码映射**（Agent 已改写入规则）。

---

## 一、Tab4 渲染问题

### 1.1 现象

「广告方向推荐」四张卡片中：

- 标题侧标签（如 `not_recommended | 5分`）往往正常；
- **卡片正文**却出现整段 JSON，包含 `suitability_score`、`push_natural`、`decision_package` 等键名。

### 1.2 根因

前端曾将 `content_json` 当 JSON 对象解析或 `JSON.stringify` 后展示，导致卡片正文出现键名。

**Agent 现行写入**：`content_json` 为 **按分号 `;` / `；` 拆分的 JSON 字符串数组**。

### 1.3 Agent 写入结构（`content_json`）

生成函数：`app/persistence/erp_writer/text_utils.py` → `build_wizard_direction_content_json()`。

| 列 | 内容 |
|----|------|
| `content_json` | JSON 数组：`["第一句", "第二句", …]`（由 `direction.reason` 按 `;` 拆分） |
| `decision_basis_json` | JSON 数组：`["第一句", …]`（由 `analysis.overall_analysis` 按 `。` / `;` 拆分） |

> **完整字段表、样例与 WHP 绑定**：见 [`广告方向推荐-ERP存储格式说明.md`](广告方向推荐-ERP存储格式说明.md)。
| `conclusion_json.direction_analyses[].analysis` | JSON 数组：`["第一句", …]`（按换行/`•`/分号拆分，去掉【】标题行） |
| `direction_type` | 方向 ERP 码，如 `OPTIMIZE_ACOS` |
| `recommend_tag` | 状态：`not_recommended` / `available` / `recommended` |
| `suggest_score` | 分数 |

示例（`content_json` 列实际存储值）：

```json
["根据近5日ACOS由30.1%升至71.6%（05-29→06-02），判断宜先控 ACOS 而非继续推自然位"]
```

### 1.4 数据来源与列绑定

| UI 区域 | 表 | 查询 |
|---------|-----|------|
| 四张方向卡 | `t_advert_agent_direction_recommend_detail` | `decision_id` = 当前 ASIN 绑定决策，`ORDER BY sort_order` |
| 决策摘要 | `t_advert_agent_direction_recommend` | 同上 |

| UI 元素 | 绑定 |
|---------|------|
| 标题 | `direction_type` → 中文（见 §1.6） |
| **正文** | **`JSON.parse(content_json)`** 得到字符串数组，逐条渲染为 bullet |
| 状态 | 列 `recommend_tag` |
| 分数 | 列 `suggest_score` |

活动级「分析建议」列表**不要**读本表，应读 `t_advert_agent_modify_suggest_card`。

### 1.5 前端修复示例

```javascript
const lines = JSON.parse(row.content_json); // string[]

// 正确
const title = mapDirectionType(row.direction_type);
const bullets = Array.isArray(lines) ? lines : [];

// 错误
// cardBody = row.content_json;
```

状态与分数优先用列：`row.recommend_tag`、`row.suggest_score`。

### 1.6 方向类型与推荐标签码表

**`direction_type`（ERP / 列值）→ 中文标题**

| ERP 码 | 中文 |
|--------|------|
| `PUSH_NATURAL` | 推进自然位 |
| `EXPAND_KEYWORDS` | 新增扩词 |
| `OPTIMIZE_ACOS` | 优化 ACOS |
| `BALANCE_MAINTAIN` | 平衡维持 |

**`recommend_tag` → 中文**

| 码 | 中文 |
|----|------|
| `not_recommended` | 暂不建议 |
| `available` | 可考虑 |
| `recommended` | 推荐 |

### 1.7 常见坑

1. **混用 decision**：仅跑 Campaign API、未带 Wizard 的写入可能使某 `decision_id` 下 **0 条** `direction_recommend_detail`。界面需绑定曾执行 `write_full`（含 wizard jsonl）的决策。
2. **旧数据含多余键**：让 Agent 重跑 `write_erp_all.py`（带 `--wizard`）刷新为 `{"reason":"…"}`。
3. **`item_count`**：当前 Agent 固定写 `1`，勿当作「N 条活动建议」条数。

### 1.8 WHP 自检清单

- [ ] Tab4 按正确 `decision_id` 查询，与 ASIN 绑定一致
- [ ] 正文绑 `JSON.parse(content_json).reason`，不要渲染整列
- [ ] 不 `JSON.stringify` 整个 `content_json`
- [ ] 活动列表改读 `modify_suggest_card`

### 1.9 刷新库内 Tab4 数据（Agent 侧）

```bash
cd ad-direction-agent
python scripts/erp_db/write_erp_all.py \
  --asin B0CGH9QRKK \
  --kb <path>/B0CGH9QRKK_kb.jsonl \
  --wizard <path>/B0CGH9QRKK_wizard.jsonl \
  --report <path>/B0CGH9QRKK_write_report.json
```

校验脚本：`python scripts/erp_db/verify_tab4_content.py --wizard <wizard.jsonl> --decision-id <id>`

---

## 二、Campaign 组合类型映射（分析建议 Tab）

Agent 将 Campaign 组合的中文标签写入 ERP 字段 **`campaign_group_type`**（Java 侧 `campaignGroupType`），供 WHP「分析建议」Tab 展示或筛选。

### 2.1 现行映射（2026-06 起）

| 中文（Agent `ai_portfolio_class` / 界面） | ERP 码 |
|----------------------------------------|--------|
| 精准主力组 | `exact_core_group` |
| 精准测试组 | `exact_testing_group` |
| 自动广泛组 | `auto_broad_group` |
| 低价捡漏组 | `low_bid_retention_group` |

常量定义：`app/workflow/steps/campaign_portfolio.py`（`PORTFOLIO_*`）。  
写入映射：`app/persistence/erp_writer/text_utils.py` → `map_campaign_group_type()`。

### 2.2 遗留数据兼容

入库时以下旧值会自动映射到上表新码（读历史 JSON / 旧分析结果无需改库逻辑）：

| 遗留中文 | 映射到 ERP 码 |
|----------|----------------|
| 主推 | `exact_core_group` |
| 广泛/自动 | `auto_broad_group` |
| 测试/新增 | `exact_testing_group` |
| 淘汰 | `low_bid_retention_group` |

| 遗留 ERP 码 | 映射到 |
|-------------|--------|
| `core` | `exact_core_group` |
| `test` | `exact_testing_group` |
| `auto_broad` | `auto_broad_group` |
| `eliminate` | `low_bid_retention_group` |
| `MAIN_PUSH` / `BROAD_AUTO` / `TEST_NEW` / `ELIMINATE_BUBBLE` | 同上四类 |

### 2.3 WHP 注意点

- **新跑的分析 + ERP 写入**会落新码；库里**未重跑**的旧行可能仍是旧码，需 Agent 重跑 `write_erp_all` 或 Campaign 分析带 `write_erp` 才会更新。
- 前端展示可用 ERP 码反查上表中文，或直接读建议卡上 Agent 写入的中文摘要字段（若有）。

单测：`tests/persistence/test_erp_enums.py` → `test_campaign_group_type_map`。

---

## 三、两件事的关系

| 问题 | 影响 Tab | 责任方 | 动作 |
|------|----------|--------|------|
| `content_json` 整列渲染 | Tab4 广告方向推荐 | WHP 前端 | 解析后绑 `.reason` |
| 组合类型枚举更名 | 分析建议（活动卡） | Agent 已改写入；WHP 展示/筛选用新 ERP 码 | 对齐 §2.1 表 |

两者**互不替代**：改 Tab4 解析不能解决组合类型显示；改组合映射不能解决 Tab4 JSON 正文问题。

---

## 四、相关代码与文档

| 说明 | 路径 |
|------|------|
| Tab4 `content_json` 构建 | `ad-direction-agent/app/persistence/erp_writer/text_utils.py` |
| Tab4 写入 | `ad-direction-agent/app/persistence/erp_writer/repository.py` |
| 组合分类常量 | `ad-direction-agent/app/workflow/steps/campaign_portfolio.py` |
| 建议卡 `campaign_group_type` | `ad-direction-agent/app/persistence/erp_writer/mappers.py` |
| Tab4 单测 | `ad-direction-agent/tests/persistence/test_erp_tab4_content.py` |
| 枚举单测 | `ad-direction-agent/tests/persistence/test_erp_enums.py` |
| 更全枚举码表 | [ERP方向枚举对照.md](./ERP方向枚举对照.md) |

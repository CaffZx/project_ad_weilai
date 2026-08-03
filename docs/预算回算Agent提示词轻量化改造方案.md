# 预算回算 Agent 提示词轻量化改造方案

> **状态**：定夺完成，可实施
> **日期**：2026-08-03
> **涉及代码**：`app/llm/kb_loader.py`、`app/llm/reasoner.py`、`app/workflow/steps/campaign_budget_reallocation.py`、`docs/knowledge_base/执行规则/23-广告组合与预算分配规则.md`

---

## 1. 背景与改造目标

当前预算回算 Agent 的系统提示词全文注入 KB23（915 行），并携带组合内全部活动明细（campaign_key / keyword_text / natural_rank / acos / search_volume），存在三个问题：

1. **系统提示词太重**：注入大量与"组合预算分配"无关的知识（商品定投子桶版本论证、与其他知识库的索引、新建活动规则、升降级规则等）。
2. **感知粒度错误**：回算 LLM 感知活动级明细，但预算回算是组合层决策。
3. **目的不清**：回算 LLM 的职责被稀释，没有突出"按实际花费分完父目标预算"。

**改造目标**：把回算 LLM 的职责收敛为一条——

> 根据 3 个活跃组合的实际花费/利用率/ACOS，把父目标预算（budget_pool）合理分配并尽量分完；表现好且已花完预算的组合优先加。

---

## 2. 职责重定义

| 职责 | 归属 |
|------|------|
| 组合预算二次分配（3 组切分） | **回算 LLM（本轮改造聚焦）** |
| 组内活动优先级排序（natural_traffic_priority_score） | 活动级分析 / 代码聚合，**回算 LLM 不感知** |
| 新建活动判断 | `new_campaign` 分析线 |
| 精准升降级 | `campaign_exact_transition` 引擎 |
| 低价捡漏 / 商品定投管理 | 代码护栏，**回算 LLM 不感知** |
| 算术（pool / delta / release / 利用率） | 代码 `aggregate()` 预算好 |

---

## 3. KB23 文件精简清单（已按反馈确认）

### 3.1 修改项

| 节 | 处理 | 状态 |
|----|------|------|
| §8.5 商品定投子桶 | 整节删除（含版本论证） | ✅ 已确认 |
| §11 示例 | 整节删除（prompt 模板已含输出格式） | ✅ 已确认 |
| §1 核心问题 | 压缩到 5 行 | ✅ 已确认 |
| §3 各节论证块（C-13 / C-18 等 "版本理由"） | 删除 | ✅ 已确认 |
| §4.3 比例失衡后果 / 瓶颈判断 | 压缩为 3 行 | ✅ 已确认 |
| §2 对象定义 | 暂且维持，不修改 | ✅ 已确认 |
| §3.1A / §3.6 / §4.1 / §7 / §10 | 文件层核心规则表不动 | ✅ 已确认 |

### 3.2 未定项（已定夺）

| 节 | 定夺结果 |
|----|---------|
| §12 与现有知识库的关系 | **切片不注入，文件保留**（2026-08-03 定夺） |

---

## 4. KB 切片范围（新增确认项）

**文件精简 ≠ 切片**。即使文件层保留，切片层只注入"组合预算分配决策"真正需要的知识。

### 4.1 切片建议（2026-08-03 定夺）

```
preset:  "budget_reallocation": ["23:5,7,3.6,4.1"]
```

| 注入节 | 内容 | 为什么需要 |
|--------|------|-----------|
| §5 本轮预算变化计算 | 定义 `group_requested_delta` / `low_bid_retention_release` 字段语义 | 两个字段保留给 LLM，需语义权威来源 |
| §7 组合预算分配优先级 | 7.1 默认比例 / 7.2 动态分配（含经营模式场景表）/ 7.3 场景 | 回算 LLM 的核心判断依据 |
| §3.6 精准主力组保护 | GROUP-BUDGET-004：预算不足时保护主力组 | 决定预算紧张时向谁倾斜 |
| §4.1 核心原则 | GROUP-BUDGET-004/005/006/009（4 条关键约束） | 分配遵循的原则（低价捡漏不参与、自然流量优先、等比例压缩） |

### 4.2 明确剔除（不属于预算分配的知识）

| 节 | 剔除理由 |
|----|---------|
| §1 核心问题 | 背景废话 |
| §2 对象定义 | 对象结构代码已定 |
| §3.1 组合判定规则 | 组合归属由 `campaign_portfolio.classify()` 判定，非 LLM 职责 |
| §3.1B 测试组预算/升组 | 精确升降级引擎职责 |
| §3.2 优先级规则 | 归属判定，代码已定 |
| §3.3 组合可空 | 数据现实，非 LLM 判断 |
| §3.4 新建精准测试 | `new_campaign` 职责 |
| §3.5 升组触发 | `campaign_exact_transition` 职责 |
| §3.7 降级路径 | `campaign_exact_transition` 职责 |
| §4.2 预算总和与真实 | 代码约束 |
| §4.3 健康比例 | 代码 `validate()` 校验 |
| §4.4 组内挪移 | 活动级，非组合分配 |
| §6 预算挪移与二次分配 | 计算过程，代码已算好 |
| §8 组合预算动作规则 | 活动级动作建议，非组合分配 |
| §9 输出字段规范 | prompt 模板已定输出格式 |
| §10 校验规则 | 代码 `validate()` 校验 |
| §11 示例 | prompt 模板已有 |
| §12 与现有知识库关系 | 纯索引无原文 |

### 4.3 §3.1A 自然流量优先级（2026-08-03 定夺：**不注入**）

§3.1A 含 natural_traffic_priority_score 评分表，依赖活动级数据（搜索量/自然位），而改造后回算 LLM 不再接收活动明细。**不注入**；原则已隐含在 §7.2 经营模式表 + §4.1 GROUP-BUDGET-006。

---

## 5. 系统提示词重构（`app/llm/reasoner.py:_BUDGET_REALLOC_PROMPT`）

### 5.1 模板改动

| 段 | 现内容 | 改为 |
|----|--------|------|
| 「输入说明」 | 描述 `groups[].campaigns[]` 活动明细（natural_rank/acos/search_volume 等） | 只描述组合级字段（见 §6） |
| 「你的任务」 | 5 条（含 §3.1A 组内优先级） | 重写为核心 4 条：① 看组合花费/利用率/ACOS ② 花完且表现好的组合优先加 ③ 在增量额度内尽量分完 budget_pool ④ 每组 reason 中文说明 |
| 「硬性约束」 | 4 条（≤ 约束） | 保留 4 条 + 新增「在 available_for_increase 额度内尽量分完 budget_pool，不留白」 |
| 「低价捡漏组」 | 详细说明固定 $1 不参与 | 精简为一句"低价捡漏组固定 $1、不参与分配、代码已处理" |

### 5.2 输出格式

**JSON 结构完全不变**（`allocation_method` + `parent` + `budget_groups[]`），下游 `validate()` / `to_budget_summary()` / 前端零改动。

---

## 6. 用户提示词 JSON 重构（`campaign_budget_reallocation.py:aggregate()`）

### 6.1 组合级 groups[]（去掉活动明细）

**现状**（含 campaigns[] 活动明细）：
```json
{
  "group": "精准主力组",
  "current_group_budget": 40.0,
  "daily_spend": 36.5,
  "group_requested_delta": 20.0,
  "new_requested_delta": 0.0,
  "campaigns": [
    {"campaign_key": "...", "keyword_text": "...", "natural_rank": 8, "acos": 0.22, ...}
  ]
}
```

**改为**（组合级）：
```json
{
  "group": "精准主力组",
  "current_group_budget": 40.0,
  "daily_spend": 36.5,
  "spend_utilization": 0.91,
  "acos_7d": 0.24,
  "group_requested_delta": 20.0,
  "new_requested_delta": 0.0
}
```

### 6.2 字段说明

| 字段 | 含义 | 数据源 | 保留 |
|------|------|--------|------|
| `current_group_budget` | 组合原预算 | portfolio MCP | ✅ |
| `daily_spend` | 组合日均花费（代码已 /7） | portfolio MCP | ✅ |
| `spend_utilization` | 组合利用率 = daily_spend / current_group_budget | 代码算好 | ✅ 新增 |
| `acos_7d` | 组合近 7 天 ACOS（比率，无除以 7 误会） | portfolio MCP | ⚠️ 见 §8-② |
| `group_requested_delta` | 组内净需求（见 §7 解释） | 代码聚合 | ✅ 保留（2026-08-03 定夺） |
| `new_requested_delta` | 其中新建活动需求 | 代码聚合 | ✅ 保留（2026-08-03 定夺） |
| ~~`campaigns[]`~~ | ~~活动明细~~ | — | **删除** |
| ~~`spend_7d`~~ | ~~近 7 天总花费~~ | portfolio MCP | **删除**（LLM 可能不除以 7 误会） |

### 6.3 parent / low_bid_group

`parent`（budget_pool / available_for_increase / low_bid_retention_release / priority_context）与 `low_bid_group` 原样保留，均不含活动明细。

---

## 7. 字段解释：`group_requested_delta` / `new_requested_delta`

这两个是**组合级净需求汇总**，不是活动明细。由代码在 `aggregate()` 中遍历累加生成（`campaign_budget_reallocation.py:161` 与 `:181`）。

### 7.1 group_requested_delta

**含义**：该组合内所有非淘汰活动本轮「建议预算 − 当前预算」的差值之和，加上新建活动的建议预算。

**计算**：
```
group_requested_delta = Σ(非淘汰活动 proposed_budget - current_budget)
                     + Σ(新建活动 proposed_budget)
```

**示例**：精准主力组 3 个存量词 + 1 个新建词：
| 词 | current | proposed | 差值 |
|----|---------|----------|------|
| 词A | 20 | 30 | +10 |
| 词B | 12 | 18 | +6 |
| 词C | 8 | 8 | 0 |
| 新建词D | — | 5 | +5（计入新建） |

`group_requested_delta = +16`，`new_requested_delta = +5`。

**作用**：告诉回算 LLM「组内活动自身想加多少」，配合利用率/ACOS 决定该组应分到多少。

### 7.2 new_requested_delta

**含义**：`group_requested_delta` 中来自本轮新建活动的部分（新建活动 current=0，proposed 全为净增）。

**作用**：区分「存量加预算」与「新建预算」。KB23 §3.1B 要求「不得为新活动稀释推词预算」——回算 LLM 知道新建部分后，可据此决定是否压缩新建额度保推词。

### 7.3 是否保留

两个都是组合级聚合（不含 campaign_key 等活动标识），不违背"不感知活动明细"原则。但保留与否取决于你希望回算 LLM 的判断依据：

- **保留**：回算 LLM 知道"组内想加多少"，结合利用率做"需求强度"判断。
- **去掉**：回算 LLM 纯按"花费/利用率/ACOS"分配，不依赖活动层 LLM 的需求信号。

见 §8-④。

---

## 8. 定夺记录（2026-08-03 全部确认）

| # | 定夺项 | 结果 |
|---|--------|------|
| ① | §3.1A 自然流量优先级 | **不注入**（评分表依赖活动级数据；原则已隐含在 §7.2 / §4.1） |
| ② | `acos_7d` | **给**（比率无除以 7 误会，LLM 判断"表现好"需要 ACOS 达标信息） |
| ③ | §12 与现有知识库的关系 | **切片不注入，文件保留** |
| ④ | `group_requested_delta` / `new_requested_delta` | **保留**（组合级需求信号；相关数据依赖与 KB 切片同步保留，即切片含 §5） |

---

## 9. 涉及文件与实施顺序

| 顺序 | 文件 | 改动 |
|------|------|------|
| 1 | `docs/knowledge_base/执行规则/23-广告组合与预算分配规则.md` | 按 §3 精简 |
| 2 | `app/llm/kb_loader.py` | preset 切片 `["23:7,3.6,4.1"]`（±3.1A） |
| 3 | `app/llm/reasoner.py` | `_BUDGET_REALLOC_PROMPT` 重写（系统提示词） |
| 4 | `app/workflow/steps/campaign_budget_reallocation.py` | `aggregate()` 去活动明细、补组合级字段 |
| 5 | `tests/` | 更新 budget realloc 相关断言 |

**实施前置**：§8 的 4 个定夺点已确认（2026-08-03），可开始实施。从第 1 步（KB23 精简）开始。

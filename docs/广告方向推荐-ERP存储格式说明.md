# 广告方向推荐 — ERP 存储格式说明

> **版本**：2026-06-05  
> **适用**：Agent ERP 写入 + WHP Tab4「广告方向推荐」对接  
> **测试库**：`192.168.2.51:3306` / `erp_agentadvert`  
> **校验样本**：ASIN `B0CGH9QRKK`  
> **写入代码**：`app/persistence/erp_writer/text_utils.py`、`repository.py` → `_upsert_wizard_direction`

本文档汇总「广告方向推荐」相关 ERP 字段的**现行存储格式**（改后契约），含表、字段、内容及 WHP 绑定说明。

---

## 一、涉及表

| 表 | UI 区域 | 说明 |
|----|---------|------|
| `t_advert_agent_direction_recommend_detail` | Tab4 四张方向卡 | 每 decision 固定 4 行，`ORDER BY sort_order` |
| `t_advert_agent_direction_recommend` | 紫底摘要（决策依据 / 建议 / 后续关注） | 每 decision 1 行 |
| `t_advert_agent_decision` | 用户已选方向 | `advert_direction_types` 仅存**已选中**方向 |

**不要**用 `direction_recommend_detail` 展示 Campaign 活动列表；活动级建议读 `t_advert_agent_modify_suggest_card`。

---

## 二、格式变更总览

| 表 | 字段 | 改前 | 改后 | 拆分规则 |
|----|------|------|------|----------|
| `direction_recommend_detail` | `content_json` | `{"reason":"句1；句2"}` 或嵌套对象 | `["句1","句2"]` | `direction.reason` 按 `;` / `；` |
| `direction_recommend` | `decision_basis_json` | `["整段一段话"]`（常 1 元素） | `["行1","行2",…]` | `analysis.overall_analysis` 按 `。` / `；` / `;` |
| `direction_recommend` | `conclusion_json.overall_analysis` | 字符串 | `["行1","行2",…]` | 同上 |
| `direction_recommend` | `conclusion_json.suggest_lines` | 无 | `["行1",…]`（**新增**） | `p3.overall_reasoning`【建议】 |
| `direction_recommend` | `conclusion_json.future_attention_lines` | 无 | `["行1",…]`（**新增**） | `p3.overall_reasoning`【后续关注】 |
| `direction_recommend` | `conclusion_json.direction_analyses[].analysis` | 长段文本 | `["行1",…]` | 换行 / `•` / `；`，去掉【】标题行 |
| `direction_recommend` | `data_focus_json` | 对象 | **未改**，仍为对象 | — |
| `decision` | `advert_direction_types` | 数组 | **未改**（仅枚举码规范化） | WHP 四方向 code |

**库内存法**：以上数组字段在 MySQL 中为 **JSON 字符串**（`TEXT` 列），例如列值为 `'["第一句","第二句"]'`。WHP 需 `JSON.parse()` 后逐条渲染。

**生成函数对照**：

| 字段 | 函数 |
|------|------|
| `content_json` | `build_wizard_direction_content_json()` |
| `decision_basis_json` | `build_wizard_decision_basis_json()` |
| `conclusion_json` | `build_conclusion_json_for_erp(analysis, wizard)` |

---

## 三、方向类型枚举

| 中文 | ERP code |
|------|----------|
| 推进自然位 | `PUSH_NATURAL` |
| 新增扩词 | `EXPAND_KEYWORDS` |
| 优化ACOS | `OPTIMIZE_ACOS` |
| 平衡维持 | `BALANCE_MAINTAIN` |

`recommend_tag`：`not_recommended` / `available` / `recommended`

---

## 四、字段明细（B0CGH9QRKK 样例）

### 4.1 `t_advert_agent_direction_recommend_detail`

| `direction_type` | `recommend_tag` | `suggest_score` | `content_json` |
|------------------|-----------------|-----------------|----------------|
| `PUSH_NATURAL` | `not_recommended` | `20` | `["根据近5日ACOS由30.1%升至71.6%（05-29→06-02），判断宜先控 ACOS 而非继续推自然位"]` |
| `EXPAND_KEYWORDS` | `available` | `60` | `["根据搜索词报告约有97个高转化词尚未收录，判断具备扩词空间","根据近5日ACOS由30.1%升至71.6%（05-29→06-02），判断可作为备选，与控 ACOS、维持稳定搭配"]` |
| `OPTIMIZE_ACOS` | `recommended` | `75` | `["根据近5日ACOS由30.1%升至71.6%（05-29→06-02），判断账户效率承压，需优化 ACOS","根据词「tights for women」近7天整体ACOS 48.9%，后3天由 42.1% 升至 58.0%（ACOS上升），判断应优先否词或降价","根据1个词高花费零转化，判断建议优先清理无效花费"]` |
| `BALANCE_MAINTAIN` | `available` | `42` | `["根据当前整体ACOS 46.9%高于目标30%，判断不宜激进扩量","根据核心词花费稳定但CVR下滑，判断宜维持现有结构、小幅优化出价"]` |

**未改列**（标量/枚举，直接绑定）：`direction_type`、`recommend_tag`、`suggest_score`、`sort_order`

---

### 4.2 `t_advert_agent_direction_recommend`

| 字段 | 内容 |
|------|------|
| **`decision_basis_json`** | `["当前处于推进期、旺季准备阶段，近5天ACOS从30.1%急剧恶化至71.6%，CVR从20.0%降至8.1%，整体ACOS达46.9%，效率承压明显","核心词「tights for women」花费占比高且ACOS持续恶化（后3天由42.1%升至58.0%），另有1个高花费零转化词需清理","同时搜索词报告中有97个高转化词未收录，具备扩词空间","综合来看，应以优化ACOS为首要任务，配合平衡维持稳定基础，并小批量试投新词以补充优质流量"]` |
| **`conclusion_json.overall_analysis`** | 同上（4 行数组） |
| **`conclusion_json.suggest_lines`** | `["优先优化ACOS控制低效花费","小批量试投97个高转化未收录词","维持基础流量稳定"]` |
| **`conclusion_json.future_attention_lines`** | `["监控核心词ACOS与CVR，7日内评估是否需进一步降价"]` |
| **`data_focus_json`** | `{"acos": 46.9, "cvr": 8.1}`（对象，未改格式） |

#### `conclusion_json.direction_analyses`

| `direction` | `analysis` |
|-------------|------------|
| `推进自然位` | `["根据近5日ACOS恶化，判断暂不宜推自然位","当前应优先控 ACOS","暂停或降低自然位相关加价","待ACOS回落至40%以下再评估推自然位"]` |
| `新增扩词` | `["根据搜索词报告约有97个高转化词尚未收录，判断具备扩词空间","根据近5日ACOS由30.1%升至71.6%，判断可作为备选，与控 ACOS、维持稳定搭配","每周小批量试投5-10个高转化未收录词，单组预算控制在$3-5","监控新词ACOS，超过目标2倍则暂停"]` |
| `优化ACOS` | `["根据近5日ACOS由30.1%升至71.6%（05-29→06-02），判断账户效率承压，需优化 ACOS","根据词「tights for women」近7天整体ACOS 48.9%，后3天由 42.1% 升至 58.0%（ACOS上升），判断应优先否词或降价","根据1个词高花费零转化，判断建议优先清理无效花费","将1个高花费零转化词加入否定词（精确匹配）","降低核心词出价约15%，观察7日ACOS是否回落至40%以下","若7天ACOS未降至40%以下，需进一步降价或暂停低效词"]` |
| `平衡维持` | `["根据当前整体ACOS 46.9%高于目标，判断宜维持现有活动结构","根据CVR下滑但花费可控，判断小幅调整出价即可","维持核心词出价，暂停非核心测试组","观察7日CVR是否回升"]` |

---

### 4.3 `t_advert_agent_decision`

| 字段 | 内容 |
|------|------|
| **`advert_direction_types`** | `["OPTIMIZE_ACOS","BALANCE_MAINTAIN","EXPAND_KEYWORDS"]` |

仅存用户**已选中**的 3 个方向；detail 表固定展示全部 4 个方向（含未选中项）。

---

## 五、WHP 字段绑定

| UI 区块 | 表 | 字段 | 解析方式 |
|---------|-----|------|----------|
| 卡片标题 | `direction_recommend_detail` | `direction_type` | code → 中文（见 §三） |
| 卡片正文 | `direction_recommend_detail` | `content_json` | `JSON.parse()` → `string[]` → bullet |
| 卡片状态/分数 | `direction_recommend_detail` | `recommend_tag` / `suggest_score` | 直接读列 |
| 紫底·决策依据 | `direction_recommend` | `decision_basis_json` | `JSON.parse()` → `string[]` |
| 紫底·建议 | `direction_recommend` | `conclusion_json.suggest_lines` | `JSON.parse(conclusion_json)` |
| 紫底·后续关注 | `direction_recommend` | `conclusion_json.future_attention_lines` | 同上 |
| 分方向详情（若展示） | `direction_recommend` | `conclusion_json.direction_analyses[].analysis` | 每项为 `string[]` |
| 已选方向 | `decision` | `advert_direction_types` | `JSON.parse()` → code 数组 |

### 前端示例

```javascript
// Tab4 卡片正文
const lines = JSON.parse(row.content_json); // string[]

// 紫底决策依据
const basis = JSON.parse(recommendRow.decision_basis_json);

// 紫底建议 / 后续关注
const conclusion = JSON.parse(recommendRow.conclusion_json);
const suggestLines = conclusion.suggest_lines ?? [];
const futureLines = conclusion.future_attention_lines ?? [];
```

### 不改前端时的表现

改过的字段在页面上仍会显示为 **原始 JSON 数组文本**（`["第一行","第二行"]`），不会自动变成 bullet。需 WHP 按上表解析后渲染。

---

## 六、相关文档与样例文件

| 文件 | 说明 |
|------|------|
| `docs/ERP方向枚举对照.md` | 方向 / 目的 / 关键词等枚举码 |
| `docs/WHP-Tab4渲染与组合枚举说明.md` | Tab4 渲染问题与组合枚举 |
| `docs/ERP写入说明.md` §4.2 | `write_full()` 全量写入说明 |
| `cursor临时文件/B0CGH9_direction_format_full.json` | 本文档样例的完整 JSON |
| `tests/persistence/test_erp_tab4_content.py` | 格式单测 |

---

## 七、变更记录

| 日期 | 变更 |
|------|------|
| 2026-06-04 | `content_json` 由 `{"reason":…}` 改为 `string[]`（按 `;` 拆分） |
| 2026-06-04 | `direction_analyses[].analysis` 改为 `string[]` |
| 2026-06-05 | `decision_basis_json` / `overall_analysis` 增加按 `。` 拆分；新增 `suggest_lines`、`future_attention_lines` |

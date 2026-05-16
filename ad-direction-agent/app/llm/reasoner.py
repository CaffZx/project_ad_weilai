"""LLM Reasoner — 构造 Prompt 并解析 LLM 分析结果

职责:
1. 将规则引擎产出的结构化数据组装为 LLM 提示词
2. 调用 DeepSeek 获取分析结果
3. 解析 JSON 响应为结构化分析报告
"""

import json
import logging

from app.llm.client import DeepSeekClient

logger = logging.getLogger(__name__)


SYSTEM_PROMPT = """你是一个资深的亚马逊广告运营专家（广告投手），擅长分析 ASIN 广告数据并提供可执行的优化建议。

你的任务是基于系统提供的结构化分析数据，生成一份简洁、精准的广告方向综合分析报告。

重要：所有输出内容必须使用中文，禁止出现英文单词。方向ID仅用于内部字段传递，报告中涉及方向名称时必须用中文。

## 背景知识：4 个广告方向定义

| 方向 | 适用场景 | 核心目标 |
|------|----------|----------|
| 推进自然位 | 上升词多、广告依赖词突出、核心位未进首页 | 倾斜预算推自然排名 |
| 新增扩词 | 关键词覆盖少、有高转化未收录词、现有词 ACOS 健康 | 拓展关键词覆盖 |
| 优化ACOS | ACOS 超标、有高花费零转化词、有 Bid 下调空间 | 降低 ACOS 提升效率 |
| 平衡维持 | 指标稳定、无异常信号、排名稳固 | 维持现状微调 |

## 全部校验规则（18 条）

### 推进自然位（PN-1 ~ PN-5）

- **PN-1 上升/优质词检测**: 检查 rank_change_14d ≥ 3 或 natural_rank ≤ 20 的词是否 ≥ 2 个。≥ 2 → confirmed，否则 suggest_optimize
- **PN-2 预算倾斜**: 检查子选项 budget_ratio 是否 > 50%。> 50% → suggest_optimize
- **PN-3 广告依赖词**: 检查有无 ACOS ≤ 30% 但自然位 > 20、花费 ≥ 50 的词。有 → confirmed（可推自然位降低广告依赖）
- **PN-4 上升词 ACOS**: 上升词中是否有 ACOS > 35% 的。有 → suggest_optimize（先优化再推）
- **PN-5 核心排名**: 最佳自然位 ≥ 5 → confirmed（有上升空间），已进 TOP 5 → suggest_optimize

### 新增扩词（KE-1 ~ KE-4）

- **KE-1 可用新词**: available_new_keywords ≥ 2 → confirmed，否则 suggest_optimize
- **KE-2 覆盖率**: keyword_count < 10 → confirmed（偏低可扩），否则 suggest_optimize
- **KE-3 现有词健康度**: 现有词平均 ACOS < ACOS目标×1.2 → confirmed（可承载新词）
- **KE-4 新增数量合理性**: 子选项 target_count > 15 → force_correct；> 推荐值2倍 → suggest_optimize

### 优化 ACOS（OA-1 ~ OA-4）

- **OA-1 ACOS 超标**: 关键词 ACOS ≥ 50% → force_correct；30%~50% → suggest_optimize；均 < 30% → confirmed
- **OA-2 高花费零转化**: 存在 spend ≥ 15 且 orders=0 的词 → force_correct
- **OA-3 Bid 空间**: 有 bid > 实际CPC×1.5 的词 → confirmed（有下调空间）
- **OA-4 否定词机会**: 有 impressions ≥ 500 且 clicks>0 且 orders=0 的词 → confirmed

### 平衡维持（BM-1 ~ BM-5）

- **BM-1 波动检测**: ACOS 波动 > 15% 或销量波动 > 15% → suggest_optimize；无7d数据时检查14d ACOS 绝对值
- **BM-2 异常信号**: 库存为 0 或竞品价格低于我方 20% 以上 → suggest_optimize
- **BM-3 参数合理性**: ACOS 超出目标 [25×0.5, 25×1.2] 范围 → suggest_optimize
- **BM-4 排名稳定**: 有词 rank_change_14d ≥ 5 → suggest_optimize；数据不足时检查自然位覆盖率
- **BM-5 子选项阈值**: acos_tolerance < 3 或 > 20 → suggest_optimize

### 标签联动约束

- 测试阶段 product_stage 不得为 盈利 → force_correct
- 清货阶段 product_stage 下 ad_purpose 不能为 排名型 → force_correct
- 测试阶段使用 Broad 大词 → force_correct

## 方向评分逻辑（Recommender）

系统已给出 4 方向的适配度评分（0-100），你不需要重新计算，但需要理解评分逻辑以便给出有洞察的分析：

- **推进自然位**: 上升/优质词数×8（上限30）+ 广告依赖词≥2（+20）+ 核心未进前5（+15）- 已进前5（-10）
- **新增扩词**: 可用新词×10（上限30）+ 覆盖<10词（+20）+ 平均ACOS健康（+15）+ 非盈利目的（+10）
- **优化ACOS**: ACOS>30（+30）+ 超标词×8（上限25）+ 零转化词（+20）+ Bid空间（+10）
- **平衡维持**: 基值50 + ACOS波动<10%（+15）+ ACOS在目标内（+10）+ 库存充足（+10）+ 排名稳定（+5）- 扩词机会（-15）- 超标词（-10）

分级规则：≥70 → recommended；40~69 → available；<40 → not_recommended

## 产品阶段与策略影响

- **测试**: 优先 引流型/排名型，不宜 盈利型，不宜 Broad 大词
- **推进**: 可推自然位、扩词，ACOS容忍度可适当放宽
- **收割/维持**: 优先 盈利/平衡维持，关注 ACOS 效率
- **清货**: 清库存导向，不宜大幅投入广告

## 输出格式要求

请严格按照以下 JSON 格式输出，不要包含任何 markdown 代码块标记：

{
  "overall_analysis": "整体评估，100-200字，概括ASIN健康度、核心问题、推荐方向",
  "direction_analyses": [
    {
      "direction": "方向ID",
      "analysis": "该方向的分析，50-100字，结合规则结果和业务背景",
      "suggestions": ["具体建议1，参照规则内置的建议模板"]
    }
  ],
  "action_priorities": [
    {
      "action": "行动描述",
      "priority": "高/中/低",
      "expected_impact": "预期效果"
    }
  ],
  "risk_warnings": ["基于规则结果和产品阶段的风险提示"]
}

## 分析原则
- 基于数据说话，不要泛泛而谈
- 建议要具体、可执行，参照规则内置的建议模板
- 指出矛盾点（如推进自然位 vs 优化ACOS 的权衡）
- 注意产品阶段对策略的影响，不同阶段侧重点不同
- 结合多条规则的叠加结果判断优先级（force_correct > suggest_optimize > confirmed）"""


TACTICS_SYSTEM_PROMPT = """你是一个资深的亚马逊广告运营专家。基于产品的战略定位和诊断数据，为运营人员推荐广告策略（广告目的 + 关键词类型）。

重要：所有输出内容必须使用中文，禁止出现英文单词。广告目的ID仅用于 ad_purposes 字段值（这些是内部标识不可改），reasoning 文本中必须用中文。

## 广告目的（可多选）

| 目的 | 适用场景 |
|------|----------|
| 引流型 | 需要曝光和流量、新品/冷启动、旺季准备引流 |
| 转化 | 有基础数据、Listing已优化、需要提升转化率 |
| 排名型 | 推进阶段产品、需要提升自然排名、核心词未进首页 |
| 盈利 | 收割/维持阶段、ACOS控制优先、盈利导向 |
| 清货型 | 清货阶段、库存积压、砍预算快速清库存 |

## 关键词类型（可多选，受广告目的影响）

| 类型 | 适用场景 | 关联目的 |
|------|----------|----------|
| 大词 | 需要大量曝光、预算充足 | 引流型 |
| 长尾词 | 精准转化、ACOS控制 | 转化型, 盈利型 |
| 竞品词 | 截流竞品、提升市场份额 | 引流型, 排名型 |
| 品牌词 | 防守自有流量、防止截流 | 转化型, 盈利型 |
| 自定义 | 运营有特定关键词策略 | 各目的 |

## 推荐逻辑

1. 产品阶段主导广告目的：
   - 测试 → 引流型 + 排名型 为主
   - 推进 → 排名型 + 转化型 为主
   - 收割/维持 → 转化型 + 盈利型 为主
   - 清货 → 清货型 或 转化型 + 盈利型 快速清库存

2. 淡旺季调整：
   - 旺季准备 → 需提前1-2周布局 卡位
   - 大旺季 → 引流 最大化曝光
   - 淡季 → 控制预算，盈利优先

3. 产品定位调整：
   - 头部品 → 各阶段均可多选，防守为主
   - 腰部品 → 平衡投入产出
   - 长尾品 → 控制成本，长尾词+精准转化为主

4. 广告目的影响关键词类型选择：
   - 引流型 → 大词、竞品词
   - 转化型 → 长尾词、品牌词
   - 排名型 → 大词、长尾词（精准卡位）
   - 盈利型 → 长尾词、品牌词

## 输出格式

请严格按照以下JSON格式输出，不要包含markdown代码块：

{
  "ad_purposes": ["引流型", "排名型"],
  "keyword_types": ["长尾词", "竞品词"],
  "reasoning": "基于当前产品处于推进+旺季准备，建议以排名型和引流型为主要目的，配合长尾词精准卡位和竞品词截流...",
  "tips": ["测试阶段不宜选盈利", "测试阶段不宜选大词", "大词需注意ACOS控制"]
}"""


EXECUTION_SYSTEM_PROMPT = """你是一个资深的亚马逊广告运营专家。基于产品的战略定位、广告策略和诊断数据，为运营人员推荐广告执行方向。

重要：所有自由文本输出（reasoning、conflict_notes等）必须使用中文，禁止出现英文。方向ID仅用于 recommended_directions/priority_order 字段值（这些是内部标识不可改），但在推理文本中一律用中文方向名。

## 四个执行方向

| 方向 | 适用场景 | 核心动作 |
|------|----------|----------|
| 推进自然位 | 上升词多、广告依赖词突出、核心位未进首页 | 倾斜预算推自然排名 |
| 新增扩词 | 关键词覆盖少、有高转化未收录词、现有词ACOS健康 | 拓展关键词覆盖 |
| 优化ACOS | ACOS超标、有高花费零转化词 | 降低ACOS提升效率 |
| 平衡维持 | 指标稳定、无异常信号 | 维持现状微调 |

## 推荐逻辑

1. 策略层（广告目的）→ 执行方向映射：
   - 选了 排名型 → 推进自然位 应被优先考虑
   - 选了 引流型 → 新增扩词 应被优先考虑
   - 选了 转化/盈利 → 优化ACOS, 平衡维持 应被优先考虑

2. 战略层 → 执行方向约束：
   - 测试 → 不适合 平衡维持（尚无稳定基线）
   - 推进 → 推进自然位 + 新增扩词 优先
   - 收割/维持 → 优化ACOS + 平衡维持 优先
   - 清货 → 不适合 新增扩词（不应拓新）
   - 旺季准备 → 新增扩词 为旺季储备流量
   - 淡季 → 平衡维持 + 优化ACOS 控制成本

3. 诊断数据 → 执行方向可行性：
   - ACOS > 30% → 优化ACOS 优先级提升
   - 可用新词 ≥ 2 → 新增扩词 可行
   - 核心词未进首页 → 推进自然位 可行
   - 数据稳定无异常 → 平衡维持 可行

4. 各方向之间非互斥，可多选，但要指出优先级和潜在矛盾（如 推进自然位 和 优化ACOS 可能冲突）

## 输出格式

请严格按照以下JSON格式输出，不要包含markdown代码块：

{
  "recommended_directions": ["push_natural", "expand_keywords"],
  "reasoning": "产品处于推进+旺季准备，且有上升词和广告依赖词，建议优先推进自然位和新增扩词...",
  "priority_order": ["push_natural", "expand_keywords", "optimize_acos", "balance_maintain"],
  "conflict_notes": "推进自然位可能短暂拉高ACOS，与优化ACOS存在矛盾，建议设置ACOS容忍上限"
}"""


P3_RECOMMEND_SYSTEM_PROMPT = """你是一个资深的亚马逊广告运营专家。基于产品的完整诊断数据和历史操作记录，同时给出目标ACOS和预算/Bid推荐。

重要：所有输出内容必须使用中文，禁止出现英文单词。数值字段使用英文key是内部格式需要。

## 你的决策依据

### 1. 战略与策略上下文
- 产品阶段决定了投入力度：测试→谨慎；推进→积极；收割/维持→注重效率；清货→以清库存优先
- 广告目的影响ACOS容忍度：引流型/排名型可放宽；转化型/盈利型需收紧
- 淡旺季影响预算：旺季准备→提前加预算布局；大旺季→最大化曝光；淡季→控制预算

### 2. 诊断数据
- 当前ACOS、精准ACOS、非精准ACOS、TACOS
- CPC、CTR、CVR
- 自然单占比（高→广告ACOS可放宽；低→需保守）
- 日均销量、毛利率、库存量
- ACOS趋势（近期是在改善还是恶化）
- 花费率（实际花费/预算）——高→建议增加预算；低→可缩减

### 3. 关键词级数据
- Top关键词的bid vs 实际CPC：bid远高于CPC→建议降bid；bid低于CPC且ACOS健康→可提bid
- 各关键词的ACOS、CVR表现
- 精准vs非精准投放的效率对比

### 4. 历史调整记录
- 最近7天运营手动设定的目标ACOS值和日预算值
- 如果历史中有多次调整，说明运营在试探，建议综合趋势给出方向
- 如果最近的调整刚刚生效（1-2天内），建议不要大幅偏离，渐进调整

### 5. 当前生效设定（运营手动覆盖）
- 用户 prompt 中可能包含「当前生效设定」章节，这是运营已手动配置并正在使用的值
- 如果当前设定值合理（与诊断数据匹配），应建议维持现有设定，reasoning 中说明"当前设定合理，无需调整"
- 如果当前设定值与诊断数据存在明显偏差（如目标ACOS远高于或远低于合理范围），应给出调整建议并解释原因
- 严禁忽略当前设定直接给出一个全新值——你的推荐应基于当前设定进行微调或确认

## 输出格式

请严格按照以下JSON格式输出：

{
  "target_acos": {
    "recommended_target": 25,
    "reasoning": "基于产品处于收割阶段+盈利目的+当前ACOS 28%，建议目标ACOS 25%...",
    "confidence": "high"
  },
  "budget_bid": {
    "current": 65.8,
    "suggested": 80.0,
    "direction": "increase",
    "magnitude_pct": 21.5,
    "reason": "当前花费率85%偏高，且处于推进阶段，建议适当增加预算...",
    "bid_adjustments": [
      {
        "keyword": "fishnet stockings",
        "current_bid": 0.85,
        "suggested_bid": 0.65,
        "direction": "decrease",
        "magnitude_pct": 23.5,
        "reason": "Bid远高于实际CPC($0.42)，有下调空间"
      }
    ]
  },
  "overall_reasoning": "综合评估，该ASIN处于收割阶段，建议收紧ACOS目标至25%，同时适度增加预算以维持排名...",
  "risk_warnings": ["精准ACOS 36%偏高，需重点优化精准投放", "库存仅15天，注意补货节奏"]
}

（以上JSON中的所有数值和文本均为格式示例，请根据实际输入数据计算并填充真实值，请根据你实际的分析建议填充文本，不要被格式示例内的内容误导。）

## 字段语义说明
- budget_bid.current: **日均实际花费**（≈近7天花费÷7），不是活动预算上限。从诊断数据中的"日均花费"字段取值。
- budget_bid.suggested: 建议调整后的日均花费目标值
- target_acos.recommended_target: 建议的ACOS目标百分比

## 约束规则
- 目标ACOS必须 ≥ 5% 且 ≤ 100%，精度到 1%（如 23% 而非 25%）
- 硬约束：除非产品阶段为"清货期"或"测试期"，目标ACOS相对于当前ACOS的相对变化幅度不超过 ±40%
  - 相对变化 = |推荐值 - 当前值| / 当前值 × 100%
  - 例：当前ACOS 40%，推荐 20% → 相对变化 50%（违规，应调整为 ≥24%）
- 软约束：目标ACOS大幅偏离当前值会造成广告数据剧烈波动（流量断崖、排名骤降），
  即使是合规范围内的调整，也应遵循渐进原则，避免一次性跨越过大
- 预算建议幅度单次不超过 ±30%
- Bid调整建议不超过 ±25%
- 如果数据不足以支撑判断，confidence设为"low"并在reasoning中说明
- 最多推荐5个关键词的Bid调整

## reasoning 文案要求（面向运营人员）
- 禁止在 reasoning 中提及任何内部约束规则词汇（如"硬约束""相对变化""合规范围""违规"等），
  这些是系统内部规则，运营不需要也不应该看到。用自然语言表达你的判断即可。
- 运营人员衡量广告健康度的核心指标是 TACOS（广告花费/总销售额），而非 ACOS。
  TACOS < 毛利率 才代表广告在盈利。ACOS 只看广告部分，自然单贡献不在其中。
  在 reasoning 中判断推荐值时，应引用 TACOS 而非单纯对比 ACOS 和毛利率。
- reasoning 应当具体、有说服力，包含以下要素：
  1. 当前状态判断（阶段+目的+数据表现，一句话概括）
  2. 为什么推荐这个值（结合自然单占比、趋势、库存等至少 2 个维度）
  3. 调整节奏建议（一步到位还是分步走）
  文案长度建议 80-150 字，避免泛泛而谈。
"""


class LLMReasoner:
    """LLM 推理器 —— 组装上下文并调用大模型"""

    def __init__(self, client: DeepSeekClient | None = None):
        self.client = client or DeepSeekClient()

    @staticmethod
    def _parse_json(raw: str) -> dict:
        """从 LLM 响应中提取 JSON，兼容 markdown 代码块包裹"""
        import json as _json
        import re
        raw = raw.strip()
        m = re.search(r"```(?:json)?\s*([\s\S]*?)```", raw)
        if m:
            raw = m.group(1).strip()
        return _json.loads(raw)

    def _build_context(self, data_summary: dict, scores: list[dict],
                       validations: dict, decisions: dict,
                       asin: str,
                       strategy: dict | None = None,
                       tactics: dict | None = None) -> str:
        """将结构化数据组装为 LLM 可读的上下文文本"""
        parts = []

        # 结构化字段列表（不进入基础信息循环）
        STRUCTURED_KEYS = {
            "high_acos_keywords", "rising_keywords", "wasteful_keywords",
            "top_cvr_keywords", "competitor_summary", "placement_comparison",
            "data_completeness",
        }

        # 战略层上下文
        if strategy:
            parts.append("## 战略层（人工选择）")
            parts.append(f"  - 产品定位: {strategy.get('product_level', '?')}")
            parts.append(f"  - 产品阶段: {strategy.get('product_stage', '?')}")
            parts.append(f"  - 淡旺季: {strategy.get('season_stage', '?')}")
            parts.append("")

        # 策略层上下文
        if tactics:
            parts.append("## 策略层（人工选择）")
            parts.append(f"  - 广告目的: {tactics.get('ad_purposes', [])}")
            parts.append(f"  - 关键词类型: {tactics.get('keyword_types', [])}")
            parts.append("")

        # ASIN 基本信息（仅标量字段）
        parts.append("## ASIN 基本信息")
        if data_summary:
            lines = []
            for k, v in data_summary.items():
                if k in STRUCTURED_KEYS:
                    continue
                lines.append(f"  - {k}: {v}")
            parts.append("\n".join(lines))

        # ── 关键词级洞察 ──
        if data_summary.get("high_acos_keywords"):
            parts.append("\n## 高ACOS关键词 TOP5")
            for kw in data_summary["high_acos_keywords"]:
                parts.append(
                    f"  - [{kw['match_type']}] {kw['keyword']}: "
                    f"ACOS {kw['acos']}%, 花费 ${kw['spend']}, 订单 {kw['orders']}单"
                )

        if data_summary.get("rising_keywords"):
            parts.append("\n## 上升关键词 TOP5（排名正向变化）")
            for kw in data_summary["rising_keywords"]:
                rank_info = f"当前第{kw['natural_rank']}位" if kw.get("natural_rank") else "排名未知"
                acos_info = f"ACOS {kw['acos']}%" if kw.get("acos") else ""
                parts.append(
                    f"  - {kw['keyword']}: 上升{kw['rank_change']}位, {rank_info}"
                    f"{', ' + acos_info if acos_info else ''}"
                )

        if data_summary.get("wasteful_keywords"):
            parts.append("\n## 高花费零转化词（需关注）")
            for kw in data_summary["wasteful_keywords"]:
                parts.append(
                    f"  - [{kw['match_type']}] {kw['keyword']}: "
                    f"花费 ${kw['spend']}, 曝光 {kw['impressions']}, 订单 0"
                )

        if data_summary.get("top_cvr_keywords"):
            parts.append("\n## 高转化关键词 TOP5")
            for kw in data_summary["top_cvr_keywords"]:
                acos_info = f"ACOS {kw['acos']}%" if kw.get("acos") else "ACOS 无数据"
                parts.append(
                    f"  - {kw['keyword']}: CVR {kw['cvr']}%, "
                    f"{acos_info}, {kw['orders']}单"
                )

        # ── 竞品摘要 ──
        comp = data_summary.get("competitor_summary")
        if comp:
            parts.append("\n## 竞品对比")
            parts.append(f"  - 竞品数量: {comp.get('competitor_count', '?')}个")
            if comp.get("price_range"):
                parts.append(f"  - 竞品价格区间: {comp['price_range']}")
                parts.append(f"  - 我方价格: ${comp.get('our_price', '?')}")
            if comp.get("price_position"):
                parts.append(f"  - 价格定位: {comp['price_position']}")
            if comp.get("avg_rating"):
                parts.append(f"  - 平均评分: {comp['avg_rating']}")

        # ── 广告位对比 ──
        placement = data_summary.get("placement_comparison")
        if placement:
            parts.append("\n## 广告位效率对比（精准 vs 非精准）")
            t_acos = placement.get("tos_acos")
            r_acos = placement.get("ros_acos")
            if t_acos is not None and r_acos is not None:
                diff = r_acos - t_acos
                note = f"（非精准ACOS比精准高{diff:.1f}个百分点）" if diff > 0 else ""
                parts.append(f"  - 精准ACOS: {t_acos}%, 非精准ACOS: {r_acos}% {note}")
            if placement.get("tos_cpc") is not None:
                parts.append(f"  - 精准CPC: ${placement['tos_cpc']}, 非精准CPC: ${placement['ros_cpc']}")
            if placement.get("tos_spend_ratio") is not None:
                parts.append(f"  - 精准花费占比: {placement['tos_spend_ratio']}%, "
                             f"非精准花费占比: {placement['ros_spend_ratio']}%")

        # 方向评分
        parts.append("\n## 各方向适配度评分")
        for s in scores:
            parts.append(
                f"  - {s.get('label', s.get('id', '?'))}: "
                f"{s.get('suitability_score', '?')}分 "
                f"({s.get('suitability', '?')})"
            )
            if s.get("reason"):
                parts.append(f"    理由: {s['reason']}")

        # 规则校验结果
        parts.append("\n## 规则校验结果")
        dir_labels = {"push_natural": "推进自然位", "expand_keywords": "新增扩词", "optimize_acos": "优化ACOS", "balance_maintain": "平衡维持"}
        for direction, result in validations.items():
            items = (result or {}).get("items", []) or []
            if not items:
                continue
            parts.append(f"\n### {dir_labels.get(direction, direction)}")
            for item in items:
                level = item.get("level", "?")
                rid = item.get("rule_id", "?")
                msg = item.get("message", "?")
                parts.append(f"  [{level}] {rid}: {msg}")

        # 决策任务
        parts.append("\n## 决策执行任务")
        dir_labels = {"push_natural": "推进自然位", "expand_keywords": "新增扩词", "optimize_acos": "优化ACOS", "balance_maintain": "平衡维持"}
        for direction, result in decisions.items():
            pkg = (result or {}).get("decision_package", {}) or {}
            tasks = pkg.get("tasks", []) or []
            if not tasks:
                continue
            parts.append(f"\n### {dir_labels.get(direction, direction)}")
            for t in tasks:
                pri = t.get("priority", "?")
                act = t.get("action", "?")
                det = t.get("details", "")
                parts.append(f"  [{pri}] {act}")
                if det:
                    parts.append(f"    {det}")

        return "\n".join(parts)

    async def recommend_tactics(
        self,
        asin: str,
        data_summary: dict,
        strategy: dict,
    ) -> dict:
        """基于战略层选择 + 诊断数据，推荐广告目的和关键词类型"""
        STRUCTURED_KEYS = {
            "high_acos_keywords", "rising_keywords", "wasteful_keywords",
            "top_cvr_keywords", "competitor_summary", "placement_comparison",
            "data_completeness",
        }

        context_parts = [
            "## 战略层选择",
            f"  - 产品定位: {strategy.get('product_level', '?')}",
            f"  - 产品阶段: {strategy.get('product_stage', '?')}",
            f"  - 淡旺季: {strategy.get('season_stage', '?')}",
            "",
            "## 诊断数据摘要",
        ]
        for k, v in data_summary.items():
            if k in STRUCTURED_KEYS:
                continue
            context_parts.append(f"  - {k}: {v}")

        # 关键词级洞察（精简版）
        if data_summary.get("high_acos_keywords"):
            context_parts.append("\n### 高ACOS关键词")
            for kw in data_summary["high_acos_keywords"][:3]:
                context_parts.append(f"  - {kw['keyword']}: ACOS {kw['acos']}%")
        if data_summary.get("rising_keywords"):
            context_parts.append("\n### 上升关键词")
            for kw in data_summary["rising_keywords"][:3]:
                context_parts.append(f"  - {kw['keyword']}: 上升{kw['rank_change']}位")
        if data_summary.get("competitor_summary"):
            comp = data_summary["competitor_summary"]
            context_parts.append(f"\n### 竞品: {comp.get('competitor_count', '?')}个, "
                                 f"价格{comp.get('price_range', '?')}")

        messages = [
            {"role": "system", "content": TACTICS_SYSTEM_PROMPT},
            {"role": "user", "content": (
                f"请为 ASIN ({asin}) 推荐广告策略（广告目的 + 关键词类型）。\n\n"
                + "\n".join(context_parts)
            )},
        ]

        try:
            raw = await self.client.chat(
                messages=messages,
                temperature=0.3,
                response_format={"type": "json_object"},
            )
            return self._parse_json(raw)
        except Exception as e:
            logger.warning("LLM 策略推荐失败，使用规则降级: %s", e)
            return self._fallback_tactics(data_summary, strategy)

    def _fallback_tactics(self, data_summary: dict, strategy: dict) -> dict:
        """规则降级：不依赖LLM的策略推荐"""
        stage = strategy.get("product_stage", "")
        season = strategy.get("season_stage", "")

        purposes = []
        if stage in ("测试期", "起步期", "测试"):
            purposes = ["引流型", "排名型"]
        elif stage in ("推进期", "进展期", "冲刺期", "推进"):
            purposes = ["排名型", "转化型"]
        elif stage in ("收割利润期", "达成期", "超预期", "收割", "维持", "维持期"):
            purposes = ["转化型", "盈利型"]
        elif stage in ("清货期", "清货中/淘汰", "清货"):
            purposes = ["转化型", "盈利型"]
        else:
            purposes = ["引流型", "转化型"]

        if season in ("大旺季", "旺季准备"):
            if "引流型" not in purposes:
                purposes.insert(0, "引流型")
        elif season == "淡季":
            purposes = [p for p in purposes if p != "引流型"] or ["盈利型"]

        keyword_types = []
        if "引流型" in purposes:
            keyword_types.append("大词")
        if "排名型" in purposes:
            keyword_types.extend(["长尾词", "竞品词"])
        if "转化型" in purposes or "盈利型" in purposes:
            keyword_types.append("长尾词")
        if "品牌词" not in keyword_types:
            keyword_types.append("品牌词")

        return {
            "ad_purposes": purposes,
            "keyword_types": list(set(keyword_types)),
            "reasoning": "(规则降级推荐) 基于产品阶段和淡旺季自动推导",
            "tips": [],
        }

    async def recommend_execution(
        self,
        asin: str,
        data_summary: dict,
        strategy: dict,
        tactics: dict,
        scores: list[dict],
    ) -> dict:
        """基于全上下文推荐执行方向"""
        context_parts = [
            "## 战略层",
            f"  - 产品定位: {strategy.get('product_level', '?')}",
            f"  - 产品阶段: {strategy.get('product_stage', '?')}",
            f"  - 淡旺季: {strategy.get('season_stage', '?')}",
            "",
            "## 策略层",
            f"  - 广告目的: {tactics.get('ad_purposes', [])}",
            f"  - 关键词类型: {tactics.get('keyword_types', [])}",
            "",
            "## 方向评分",
        ]
        for s in scores:
            context_parts.append(
                f"  - {s.get('label', s.get('id', '?'))}: "
                f"{s.get('suitability_score', '?')}分 ({s.get('suitability', '?')})"
            )

        messages = [
            {"role": "system", "content": EXECUTION_SYSTEM_PROMPT},
            {"role": "user", "content": (
                f"请为 ASIN ({asin}) 推荐广告执行方向。\n\n"
                + "\n".join(context_parts)
            )},
        ]

        try:
            raw = await self.client.chat(
                messages=messages,
                temperature=0.3,
                response_format={"type": "json_object"},
            )
            return self._parse_json(raw)
        except Exception as e:
            logger.warning("LLM 执行推荐失败，使用评分降级: %s", e)
            # 降级：选评分最高的方向
            top = sorted(scores, key=lambda s: s.get("suitability_score", 0), reverse=True)
            recommended = [s["id"] for s in top if s.get("suitability_score", 0) >= 40][:2]
            return {
                "recommended_directions": recommended or ["balance_maintain"],
                "reasoning": "(规则降级) 基于适配度评分排序推荐",
                "priority_order": [s["id"] for s in top],
                "conflict_notes": "",
            }

    async def recommend_p3(
        self,
        asin: str,
        strategy: dict,
        tactics: dict,
        data_summary: dict,
        keyword_details: list[dict],
        history: list[dict],
        current_acos_target: int | None = None,
        current_daily_budget: float | None = None,
    ) -> dict:
        """P3 统一推荐：LLM 同时给出目标 ACOS 和预算/Bid 建议"""
        parts = [
            "## 战略层",
            f"  - 产品定位: {strategy.get('product_level', '?')}",
            f"  - 产品阶段: {strategy.get('product_stage', '?')}",
            f"  - 淡旺季: {strategy.get('season_stage', '?')}",
            "",
            "## 策略层",
            f"  - 广告目的: {tactics.get('ad_purposes', [])}",
            f"  - 关键词类型: {tactics.get('keyword_types', [])}",
            "",
        ]

        # 当前生效设定 — LLM 需基于现有设定判断是否需要调整
        has_current = current_acos_target is not None or current_daily_budget is not None
        if has_current:
            parts.append("## 当前生效设定（运营已手动配置）")
            if current_acos_target is not None:
                parts.append(f"  - 当前目标ACOS: {current_acos_target}%（运营手动设定）")
            if current_daily_budget is not None:
                parts.append(f"  - 当前日预算: ${current_daily_budget:.0f}（运营手动设定）")
            parts.append("  → 请基于以上当前设定判断是否需要调整。如当前设定合理可建议维持；如需调整请说明理由。")
            parts.append("")

        parts.extend([
            "## 诊断数据",
        ])
        for k, v in data_summary.items():
            if v is not None:
                parts.append(f"  - {k}: {v}")
        parts.append("")

        if keyword_details:
            parts.append("## 关键词级数据（Top关键词）")
            for kw in keyword_details[:10]:
                parts.append(
                    f"  - {kw.get('keyword', '?')}: bid=${kw.get('bid',0):.2f}, "
                    f"CPC=${kw.get('cpc',0):.2f}, ACOS={kw.get('acos',0):.0f}%, "
                    f"CVR={kw.get('cvr',0):.0f}%, spend=${kw.get('spend',0):.0f}"
                )
            parts.append("")

        if history:
            parts.append("## 历史调整记录（最近7天）")
            for h in history:
                parts.append(
                    f"  - {h.get('date', '?')}: "
                    f"目标ACOS={h.get('target_acos', '未设定')}, "
                    f"日预算={h.get('daily_budget', '未设定')}"
                )
            parts.append("")
        else:
            parts.append("## 历史调整记录\n  （无历史记录）\n")

        messages = [
            {"role": "system", "content": P3_RECOMMEND_SYSTEM_PROMPT},
            {"role": "user", "content": (
                f"请为 ASIN ({asin}) 同时给出目标ACOS和预算/Bid推荐。\n\n"
                + "\n".join(parts)
            )},
        ]

        try:
            raw = await self.client.chat(
                messages=messages,
                temperature=0.3,
                response_format={"type": "json_object"},
            )
            result = self._parse_json(raw)
            logger.info("P3 LLM 推荐成功 [%s]", asin)
            return result
        except Exception as e:
            logger.warning("P3 LLM 推荐失败 [%s]: %s", asin, e)
            raise

    async def analyze(
        self,
        asin: str,
        data_summary: dict,
        scores: list[dict],
        validations: dict,
        decisions: dict,
        strategy: dict | None = None,
        tactics: dict | None = None,
    ) -> dict:
        """综合分析入口（增强版，接收战略+策略上下文）"""
        context = self._build_context(
            data_summary=data_summary or {},
            scores=scores or [],
            validations=validations or {},
            decisions=decisions or {},
            asin=asin,
            strategy=strategy,
            tactics=tactics,
        )

        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": (
                f"请分析以下 ASIN ({asin}) 的广告数据，生成综合分析报告。\n\n"
                f"{context}"
            )},
        ]

        try:
            raw = await self.client.chat(
                messages=messages,
                temperature=0.3,
                response_format={"type": "json_object"},
            )
            result = self._parse_json(raw)
        except Exception as e:
            logger.warning("LLM 分析失败，返回降级结果: %s", e)
            result = {
                "overall_analysis": f"LLM 分析暂不可用（{e}），请稍后重试。",
                "direction_analyses": [],
                "action_priorities": [],
                "risk_warnings": ["LLM 服务异常"],
            }

        result.setdefault("direction_analyses", [])
        result.setdefault("action_priorities", [])
        result.setdefault("risk_warnings", [])
        return result


# 全局单例
reasoner = LLMReasoner()

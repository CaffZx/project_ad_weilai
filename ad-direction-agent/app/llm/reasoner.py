"""LLM Reasoner — 构造 Prompt 并解析 LLM 分析结果

职责:
1. 将规则引擎产出的结构化数据组装为 LLM 提示词
2. 调用 DeepSeek 获取分析结果
3. 解析 JSON 响应为结构化分析报告
"""

import json
import logging
import re

from app.core.metrics_ops_language import format_keyword_acos_change, humanize_ops_text, period_labels
from app.core.validation_ops import format_validation_item_ops
from app.llm.client import DeepSeekClient, deepseek_client
from app.llm.kb_loader import kb
from app.models.campaign import CampaignUnit
from app.models.layers import product_level_with_code

# 运营正文禁止出现的规则编号 / 内部等级词
_RULE_ID_PATTERN = re.compile(
    r"\b(?:PN|KE|OA|BM|PX|CROSS)[-_]?\d+\b",
    re.IGNORECASE,
)
_INTERNAL_LEVEL_PATTERN = re.compile(
    r"\b(?:confirmed|suggest_optimize|force_correct)\b",
    re.IGNORECASE,
)

OPS_WRITING_RULES = """
## 运营文案硬性要求（面向人的字段）

1. **叙事结构**：先写**趋势/变化**（近N天整体、后X天与前Y天对比、排名变化），再写**判断**。标准句式：「根据{趋势描述}，判断{结论}」。
2. **禁止**在正文出现：规则编号（KE-1、BM-4、PN-1、OA-1 及「BM-4提示」等）、**半窗/全窗/近半窗**、英文等级、门禁/eligible、**方向评分数值**。
3. 可引用数据结论，须改写成运营语言，不得照搬带编号的校验行。
4. 引用数值须带时间窗口（7日平均、5月13日当天、近3日 vs 前4日）。
"""

REASONING_DECISION_RULES = """
## reasoning 中【决策依据】专规（浅蓝综合分析，最重要）

【决策依据】须写 **3～5 条**，每条独立一行（\\n 分隔），且**每条必须**同时包含「根据」与「判断」：

格式：根据 + {趋势或变化，含时间/窗口} + ， + 判断 + {结论}

合格示例（勿照搬数值）：
- 根据近7日ACOS由22%升至27%、CVR由32%降至23%的趋势，判断效率边际走弱但仍可控。
- 根据核心词自然位稳定在约第3名、自然单占比约75%，判断推自然位性价比低，不宜作为主方向。
- 根据3个在投词ACOS超40%且最近几天ACOS持续上升，判断应优先优化ACOS。
- 根据76个高转化词未收录、在投词ACOS约28%可承载，判断旺季末期宜小批量试扩词并设ACOS上限。

不合格示例（禁止）：
- 产品处于收割利润期、旺季末期。（无趋势、无判断）
- 近4日ACOS在27-32%间波动 / ACOS在26.9%-31.8%之间波动。（区间罗列，须写「从…变化至…」或引用 daily_trend_text）
- 平衡维持82分最高，扩词55分。（罗列评分，非趋势判断）
- 整体ACOS约28%。（静态裸数值）
- 段外重复裸指标行（如单独一行「ACOS在…%-…%之间波动」）

【建议】【后续关注】：可分条写动作；条件句写清阈值。
"""

logger = logging.getLogger(__name__)


_ANALYZE_TASK_PROMPT = """你是一个资深的亚马逊广告运营专家（广告投手），擅长分析 ASIN 广告数据并提供可执行的优化建议。

重要：所有输出内容必须使用中文，禁止出现英文单词。方向ID仅用于内部字段传递，报告中涉及方向名称时必须用中文。

## 业务知识（必须严格遵循）
{kb_content}

## 输出格式要求

请严格按照以下 JSON 格式输出，不要包含任何 markdown 代码块标记。

**overall_analysis（综合概览）**：用 **3～5 句连贯段落** 概括当前 ASIN 广告整体状态（阶段、核心指标、近几日趋势、主要矛盾与方向取舍），**禁止**使用【决策依据】【建议】【后续关注】等分段标题，禁止分条罗列方向评分。

**direction_analyses[].analysis（分方向后续动作）**：使用 **两段**，用【】标注：

1. **【分析与建议】**：先写 1～3 条「根据…趋势/数据，判断…」；再写可执行动作（多条用「• 」换行）。禁止 PN/BM/OA 等规则编号。
2. **【后续关注】**：1～3 条监控条件（含阈值）。

扩词方向：若上下文提供 `expand_keyword_candidates`（≤10 个），在【分析与建议】中**必须逐个列出全部英文词**，禁止用「等」省略。

{
  "overall_analysis": "当前处于收割利润期、旺季末期，核心词自然位约第3、自然单约75%。近7日ACOS约28%且近4日从30.4%升至31.8%，CVR略降。有2个词ACOS≥50%需优化，另有76个高转化词未收录。综合宜以平衡维持为主，辅以优化ACOS与小批量试词。",
  "direction_analyses": [
    {
      "direction": "新增扩词",
      "analysis": "【分析与建议】\\n根据…判断…\\n• 试投词须全部列出：「tights sheer」、「plus size fishnet」…\\n\\n【后续关注】\\n若7日ACOS超40%则暂停…"
    }
  ],
  "action_priorities": [],
  "risk_warnings": [],
  "skip_directions_note": "可选：对未选但评分较高的方向一句说明，无则空字符串"
}

- direction 字段必须使用**中文方向名**（推进自然位、新增扩词、优化ACOS、平衡维持），禁止用 push_natural 等 ID
- action_priorities、risk_warnings 若无独立内容可留空数组，要点已写入上述三段时勿重复罗列

## 时间维度与趋势判断（选词与决策的核心）

用户消息会提供：
- **ASIN 日趋势**（`daily_trend_text`）：近 N 日 ACOS/CVR/CPC/订单/花费逐日变化。最近一天可能不完整，分析时以较早的完整日为准。
- **关键词分段对比**：高花费词带有「近N天整体」与「前X天→后Y天」的 ACOS、花费、订单及趋势标签（如 ACOS明显改善）。

**你必须：**
1. **优先依据趋势方向**做判断（改善 / 恶化 / 持平），再引用近N天整体指标作佐证；禁止只写静态 ACOS/CVR 而不说明变化方向。
2. **禁止**在输出中使用「半窗」「全窗」「BM-4」等内部术语；引用检查项时只写中文名称（如「排名稳定性」），禁止写规则编号。
3. **禁止**仅用「ACOS在 X%-Y% 之间波动」描述趋势；必须引用 `daily_trend_text` 中的「近N日ACOS从…变化至…」或「近7天整体…后4天由…降至…」。
4. 引用指标须带**时间窗口与口径**，例如：「近7天ACOS从5月13日的31.9%降至5月17日的21.3%」「该词近7天整体ACOS 45%，但后4天已由 28% 降至 22%（明显改善）」。
5. 选词举例、否定/加价建议必须来自上下文中的关键词列表，并说明**为何基于其趋势**采取该动作。
6. `keyword_trend_watch` 中的词为趋势异动重点，在分析中应优先讨论。

## 分析原则
- 基于**趋势 + 判断**叙事，不要泛泛而谈
- 建议要具体、可执行，用运营能直接执行的中文
- 指出矛盾点（如推进自然位 vs 优化ACOS 的权衡）

## 方向范围约束
- 用户消息会提供 `selected_directions`（运营已选方向）：`direction_analyses` **仅**输出这些方向，每个一条，不得展开未选方向
- 未选方向若有重要风险，最多在 `overall_analysis` 或 `skip_directions_note` 用一句话说明
- `suggestions` 应**补充**决策包中已有任务，勿与 tasks 列表矛盾重复；勿复述任务编号或规则编号
- `action_priorities` 应对齐决策包任务优先级，可细化但勿推翻数据结论""" + OPS_WRITING_RULES


def _build_analyze_system_prompt() -> str:
    return _ANALYZE_TASK_PROMPT.replace("{kb_content}", kb.build("analyze_report"))


_EXECUTION_TASK_PROMPT = """你是一个资深的亚马逊广告运营专家。基于知识库规则和用户消息中的诊断数据，推荐广告执行方向。

重要：所有自由文本输出（reasoning、conflict_notes等）必须使用中文，禁止出现英文。方向ID仅用于 recommended_directions/priority_order 字段值（这些是内部标识不可改），但在推理文本中一律用中文方向名。

## 业务知识（必须严格遵循）
{kb_content}

## 硬性约束（来自用户消息）
- recommended_directions 必须 ⊆ eligible_directions
- reasoning 引用规则时只复述检查项中文名，禁止 PN/BM/OA 等内部编号
""" + OPS_WRITING_RULES + REASONING_DECISION_RULES + """

## 输出格式

请严格按照以下JSON格式输出，不要包含markdown代码块：

{
  "recommended_directions": ["balance_maintain", "optimize_acos", "expand_keywords"],
  "reasoning": "【决策依据】\\n根据近7日ACOS由22%升至27%、CVR由32%降至23%的趋势，判断效率走弱但仍处于收割期可接受区间。\\n根据核心词自然位约第3名、自然单占比约75%，判断推自然位边际价值低，不宜作为主方向。\\n根据3个在投词ACOS超40%且花费集中，判断应优先否词或降价以控ACOS。\\n根据76个高转化词未收录、在投词整体ACOS约28%，判断可小批量试扩词但须设30%上限。\\n\\n【建议】\\n• 以平衡维持为主，维持现有结构守住排名与利润。\\n• 优化ACOS：处理超标词，整体控制在30%以内。\\n• 小批量试词5～10个，7日观察再放量。\\n\\n【后续关注】\\n若近7日ACOS持续高于35%或扩词批次7日ACOS超40%，需收紧或暂停对应动作。",
  "priority_order": ["balance_maintain", "optimize_acos", "expand_keywords", "push_natural"],
  "conflict_notes": ""
}

reasoning 必须含【决策依据】【建议】【后续关注】三段；【决策依据】每条独立一行且为「根据…，判断…」句式。"""


def _build_execution_system_prompt() -> str:
    return _EXECUTION_TASK_PROMPT.replace("{kb_content}", kb.build("execution_direction"))


_P3_TASK_PROMPT = """你是一个资深的亚马逊广告运营专家。基于知识库规则和诊断数据，同时给出目标ACOS和预算/Bid推荐。

重要：所有输出内容必须使用中文，禁止出现英文单词。数值字段使用英文key是内部格式需要。

## 业务知识（必须严格遵循）
{kb_content}

## 输出格式

请严格按照以下JSON格式输出：

{
  "target_acos": {
    "recommended_target": 25,
    "reasoning": "【决策依据】\\n- 当前ACOS 33.6%，TACOS 15.2%，毛利率 28.5%。TACOS < 毛利率，广告整体盈利。\\n- 精准ACOS偏高(42.1%)，主要拖累来自triangle bikini(ACOS 48%)和string bikini(ACOS 41%)。\\n- 自然单占比68.7%，收紧ACOS对自然流量影响有限。\\n- 趋势方向：近7天ACOS从48.3%持续降至36.2%，处于改善通道。\\n- 当前推进期+排名型为主，ACOS容忍度可适度放宽。\\n\\n【建议】\\n目标ACOS推荐25%，留有改善空间同时不冲击现有流量结构。建议分两步调整：先到30%观察3天，CVR未明显下降后再收紧至25%。\\n\\n【后续关注】\\n- 监控CVR是否随ACOS收紧而下降，若跌破8%则放宽至30%\\n- 重点监控triangle bikini和string bikini的ACOS变化，若单个词ACOS降至35%以下可进一步收紧整体目标",
    "confidence": "high"
  },
  "budget_bid": {
    "current": 65.8,
    "suggested": 80.0,
    "direction": "increase",
    "magnitude_pct": 21.5,
    "reason": "【决策依据】\\n- 当前日均花费$65.8，日预算$100，花费率65.8%，预算未吃紧。\\n- 花费最高的词：triangle bikini($36.2/天, ACOS 48%)，string bikini($20.0/天, ACOS 41%)。\\n- 趋势：近7天花费从$87逐步降至$65，不是因为预算不足而是因为部分词ACOS过高被系统自然压低。\\n- 当前推进期+旺季准备，适度加预算抢流量是合理的。\\n\\n【建议】\\n建议日预算调整为$80(+21.5%)。增量集中分配给black bikini set(ACOS 28%, CVR 12.5%)和black string bikini(ACOS 18%, CVR 15%)等高效率词，不分配给triangle bikini等高ACOS词。\\n\\n【后续关注】\\n- 加预算后监控整体ACOS是否上升，若超过40%则停止增量\\n- 监控black bikini set的ACOS和CVR，若效率下降则重新分配预算",
    "bid_adjustments": [
      {
        "keyword": "triangle bikini",
        "current_bid": 0.85,
        "suggested_bid": 0.65,
        "direction": "decrease",
        "magnitude_pct": 23.5,
        "reason": "Bid $0.85远超实际CPC $0.42，ACOS 48%偏高，有$0.20以上下调空间"
      }
    ]
  },
  "overall_reasoning": "【综合判断】\\nACOS目标和预算建议需联动：收紧ACOS降低低效花费，加预算把释放出的花费转移到高效率词上，在效率不崩的前提下抢旺季排名。\\n\\n【执行节奏】\\n建议先降triangle bikini的Bid（立即可做），观察3天整体ACOS变化后，再决定是否加预算。加预算和收紧ACOS不建议同一天操作，避免数据波动难以归因。\\n\\n【风险提示】\\n- 精准ACOS 42%偏高，若精准位持续低效建议减少TOS投放比例\\n- 旺季CPC可能上涨，需预留预算弹性空间",
  "risk_warnings": ["精准ACOS 42%偏高，需重点优化精准投放", "旺季CPC可能上涨，预留预算弹性"]
}

（以上所有文本均为格式示例，请根据实际输入数据计算填充真实值。reasoning / reason / overall_reasoning 中不要照搬示例格式内的具体数值。）

## 字段语义说明
- budget_bid.current: **日均实际花费**（≈总花费÷天数）。从诊断数据中的"日均花费"字段取值。
- budget_bid.suggested: 建议的日均花费目标值。基于"日均花费"的当前水平 + 趋势 + 阶段策略给出。
- target_acos.recommended_target: 建议的ACOS目标百分比（精度到1%，如23%而非25%）
- 关键词数据中 spend 字段: 该词在 {days} 天窗口内的总计花费，除以天数才是日均花费。

## 硬性数值约束（P3 专用，TODO 待 KB 补充后迁移）
- 目标ACOS必须 ≥ 5% 且 ≤ 100%，精度到 1%
- 硬约束：除非产品阶段为"测试期"，目标ACOS相对于当前ACOS的相对变化幅度不超过 ±40%（TODO: 待 KB 补充 ACOS 目标变化约束后迁移）
- 预算建议幅度单次不超过 ±30%
- Bid调整建议不超过 ±25%
- 如果数据不足以支撑判断，confidence设为"low"并在reasoning中说明
- 最多推荐5个关键词的Bid调整
- ⚠️ 你推荐的高效词、低效词、Bid调整词必须全部来自"关键词级数据"列表中的实际关键词，禁止编造不存在于列表中的词名

## reasoning / reason / overall_reasoning 文案要求（面向运营人员）
- 禁止提及任何内部约束规则词汇（如"硬约束""相对变化""合规""违规"等），用自然语言表达
- TACOS < 毛利率 才代表广告在盈利。引用TACOS而非单纯对比ACOS和毛利率
- reasoning 和 reason 必须包含三段（用【】标注，\\n 分隔），缺一不可：
  1. **【决策依据】**：列出导致该推荐值的决定性数据指标。必须引用具体数值。禁止泛泛而谈。引用的是真正影响决策的 2-4 个核心指标，不是罗列所有数据。
     ⚠️ 每一个引用的指标必须带时间窗口和口径，禁止只写裸数值。正确写法：
     - "{days}日平均ACOS 33.6%"、“{days}日平均CVR 11.1%”、“{days}日平均CPC $0.53”
     - 引用趋势变化时："近{days}日ACOS从[5月12日]的48.3%逐日降至[5月17日]的36.2%"
     - 引用某日当天值时："[5月17日]当天CVR 43.3%"
     - 引用关键词花费时："black bikini set {days}天总花费$238.50"
     禁止写"ACOS 33.6%"、"花费$62"等不带窗口和口径的裸数值。
  2. **【建议】**：给出具体推荐值和理由。说明做什么、为什么是这个数值，是否需要分步调整。如有当前手动设定值需对比说明。
  3. **【后续关注】**：指出需要监控的 1-3 个关键指标或条件变化，说明在什么情况下应重新调整。
- overall_reasoning 必须包含三段：
  1. **【综合判断】**：ACOS和预算联动逻辑
  2. **【执行节奏】**：分步顺序建议
  3. **【风险提示】**：2-3条关注点
- 不设字数限制，决策依据部分必须引用具体数值，禁止泛泛而谈。
"""


def _build_p3_system_prompt() -> str:
    return _P3_TASK_PROMPT.replace("{kb_content}", kb.build("p3_recommend"))


# ── Campaign 系统提示词 ────────────────────────────────────────────────────

_CAMPAIGN_SHARED_INTRO = """你是一个资深的亚马逊广告运营专家。基于知识库规则和用户消息中的活动数据，逐活动分析并给出调整/淘汰建议。

重要：所有输出内容必须使用中文（理由、证据、决策路径），JSON key 使用英文。

## 业务知识（必须严格遵循）
{kb_content}

## 策略上下文解读
用户消息中的「策略上下文」包含该 ASIN 的产品阶段、广告目的、目标 ACOS、利润率、评分、退货率、库存天数、自然单占比等信息。这些是活动分析的"背景"，不需要在每个活动中重复输出。

## 活动列表
用户消息中的「活动列表」包含每个活动的：活动名、子ASIN、关键词、匹配类型、当前 Bid、当前 Budget、上线天数(注：-1 表示未知，勿当作新活动)、7日性能指标。"""

_CAMPAIGN_EXACT_PROMPT = (
    _CAMPAIGN_SHARED_INTRO
    + """

## 角色
你正在分析**精准广告活动**（EXACT 匹配类型）。调整维度为 Budget → Bid → Placement（三广告位）。

## KB 引用指引（决策链：诊断 → 取值 → 动作）
1. **先算容忍度**: KB 17 §2 + KB 15 §4（目标ACOS + 阶段加值 + ranking/旺季/promotion 加值）。所有 ACOS 高低判断必须对比该值，禁止硬编码"ACOS>40%"。
2. **再定问题类型**: KB 17 §1（BLOCKED / SAMPLE_INSUFFICIENT / HIGH_ACOS_* / BUDGET_* / RANK_* / PLACEMENT_INEFFICIENT / ALL_HEALTHY）。
3. **查动作矩阵**: KB 17 §3 按广告方向 × 问题类型取有序动作；冲突时进 KB 17 §4 裁决；如指向淘汰，必须先走 KB 17 §7 的"淘汰前诊断路径"。
4. **取约束数值**: KB 15 §1（Bid 公式/保护规则/硬上下限）、KB 15 §2（预算范围 + 淡旺季系数）、KB 15 §3（广告位矩阵 + 阻断条件）。
5. **应用幅度系数**: 最终幅度 = KB 19 基础幅度 × KB 17 §5.2 阶段系数 × KB 17 §5.3 淡旺季系数。
6. **细化执行**: KB 22 §2（精准调整规则）、KB 19 §5/§9（广告位决策/好坏判断）、KB 21（淘汰规则）。
通用基准: KB 18 §1-4/§6、KB 19 §1-4/§6/§10、KB 22 §0/§1。

## 输出格式
{
  "campaign_adjustments": [
    {
      "campaign_name": "广告活动名称",
      "campaign_key": "活动名 × 子ASIN（唯一标识）",
      "child_asin": "B0XXXXXX",
      "keyword_text": "关键词",
      "match_type": "EXACT",
      "action": "eliminate_to_low_bid_pool",
      "direction": {"bid": "down", "budget": "down"},
      "triggered_rule": "NO_CVR_HIGH_SPEND",
      "reason": "".join(["(1) 现状诊断", "(2) 原因分析", "(3) 调整建议"]),
      "confidence": "high",
      "current_budget": 15.0, "proposed_budget": 1.0,
      "current_bid": 0.85, "proposed_bid": 0.20,
      "evidence": ["7天花费$18.5", "7天订单0，CVR=0%"],
      "placement_adjustments": [
        {"placement": "头部", "current_pct": 20, "proposed_pct": 10, "action": "下调", "evidence": "..."}
      ],
      "review_level": "MANUAL_REVIEW"
    }
  ],
  "batch_summary": {"total_analyzed": 6, "to_eliminate": 1, "to_adjust": 3, "to_keep": 2, "overall_notes": "..."}
}

## 输出约束
### 必填结构字段
- **每个活动都必须填写**: campaign_key, campaign_name, child_asin, keyword_text, match_type, action, direction, triggered_rule, current_budget, proposed_budget, current_bid, proposed_bid, evidence, review_level
- proposed_budget / proposed_bid 必须填写具体数值，禁止留 null
- 必须输出 placement_adjustments（三个广告位全部列出，无数据时维持 0%）

### 淘汰活动
- action=eliminate_to_low_bid_pool 时，proposed_budget/proposed_bid 无需填写（后端自动修正为 $1.00/$0.20）
- 必须输出 triggered_rule（如 NO_CVR_HIGH_SPEND）和 evidence

### reasoning 文案禁则
- 禁止泄露内部约束术语（Bid步长/决策矩阵/规则编号/confidence等级）
- reason 中勿出现 NO_CVR_HIGH_SPEND 等触发码标记
- reason 使用三部分结构：(1) 现状诊断 → (2) 原因分析 → (3) 调整建议"""
)

_CAMPAIGN_BROAD_PROMPT = (
    _CAMPAIGN_SHARED_INTRO
    + """

## 角色
你正在分析**广泛/词组广告活动**（BROAD / PHRASE / AUTO 匹配类型）。调整维度为 Budget → Bid → SearchTerm（否词/提词）。禁止 Placement 调整（该字段填 N/A）。

## KB 引用指引（决策链：诊断 → 取值 → 动作）
1. **先算容忍度**: KB 17 §2 + KB 15 §4（目标ACOS + 阶段加值 + ranking/旺季/promotion 加值）。所有 ACOS 高低判断必须对比该值，禁止硬编码"ACOS>40%"。
2. **再定问题类型**: KB 17 §1（BLOCKED / SAMPLE_INSUFFICIENT / CVR_WEAK / HIGH_ACOS_* / BUDGET_* / KEYWORD_POOL_DIRTY / ALL_HEALTHY）。广泛/词组活动尤其关注 KEYWORD_POOL_DIRTY、HIGH_ACOS_* 的否词治理路径。
3. **查动作矩阵**: KB 17 §3 按广告方向 × 问题类型取有序动作（广泛活动多走 §3.2 expand_keywords / §3.3 optimize_acos）；冲突时进 KB 17 §4 裁决；如指向淘汰，必须先走 KB 17 §7 的"广泛/词组淘汰前诊断路径"（先否词 → 仍无改善才降 Bid/预算 → 仍无改善才淘汰）。
4. **取约束数值**: KB 15 §1（Bid 公式/保护规则/硬上下限）、KB 15 §2（预算范围 + 淡旺季系数）。
5. **应用幅度系数**: 最终幅度 = KB 19 基础幅度 × KB 17 §5.2 阶段系数 × KB 17 §5.3 淡旺季系数。
6. **细化执行**: KB 22 §3（广泛/词组调整规则）、KB 19 §7/§8（自动广泛组/否词触发）、KB 21（淘汰规则）。
通用基准: KB 18 §1-4/§6、KB 19 §1-4/§6/§10、KB 22 §0/§1。

## 输出格式
{
  "campaign_adjustments": [
    {
      "campaign_name": "广告活动名称",
      "campaign_key": "活动名 × 子ASIN（唯一标识）",
      "child_asin": "B0XXXXXX",
      "keyword_text": "关键词",
      "match_type": "BROAD",
      "action": "eliminate_to_low_bid_pool",
      "direction": {"bid": "down", "budget": "down"},
      "triggered_rule": "IRRELEVANT_NO_IMPROVEMENT",
      "reason": "".join(["(1) 现状诊断", "(2) 原因分析", "(3) 调整建议"]),
      "confidence": "high",
      "current_budget": 10.0, "proposed_budget": 8.0,
      "current_bid": 0.50, "proposed_bid": 0.40,
      "evidence": ["7天花费$12.0", "否词5个后搜索词质量仍差"],
      "negative_keywords": [
        {"keyword": "wedding dress", "clicks_7d": 12, "orders_7d": 0, "reason": "无转化高点击"}
      ],
      "review_level": "MANUAL_REVIEW"
    }
  ],
  "batch_summary": {"total_analyzed": 6, "to_eliminate": 1, "to_adjust": 3, "to_keep": 2, "overall_notes": "..."}
}

## 输出约束
### 必填结构字段
- **每个活动都必须填写**: campaign_key, campaign_name, child_asin, keyword_text, match_type, action, direction, triggered_rule, current_budget, proposed_budget, current_bid, proposed_bid, evidence, review_level
- proposed_budget / proposed_bid 必须填写具体数值，禁止留 null
- 必须判断 negative_keywords（每轮必读搜索词报告；无 neg 词时输出空数组 []；禁止 null）

### 淘汰活动
- action=eliminate_to_low_bid_pool 时，proposed_budget/proposed_bid 无需填写（后端自动修正为 $1.00/$0.20）
- 必须输出 triggered_rule（如 IRRELEVANT_NO_IMPROVEMENT）和 evidence

### reasoning 文案禁则
- 禁止泄露内部约束术语（Bid步长/决策矩阵/规则编号/confidence等级）
- reason 中勿出现 IRRELEVANT_NO_IMPROVEMENT 等触发码标记
- reason 使用三部分结构：(1) 现状诊断 → (2) 原因分析 → (3) 调整建议"""
)


def _build_campaign_system_prompt(task_type: str = "exact") -> str:
    kb_content = kb.build("campaign_adjustment")
    template = _CAMPAIGN_EXACT_PROMPT if task_type == "exact" else _CAMPAIGN_BROAD_PROMPT
    return template.replace("{kb_content}", kb_content)


# ── Campaign 汇总合成 Prompt ─────────────────────────────────────────────────

_CAMPAIGN_SYNTHESIS_PROMPT = """你是亚马逊广告运营专家。把若干单活动调整建议合成为运营可读的「分组叙事 + 特殊调整尾部清单」。

本任务**不做广告决策**——每条建议的动作(action)和理由(reason)已在上游确定。你只需读懂已有结论，按语义把"同动作 + 同理由"的建议聚类，并为每组写运营叙事。无需任何额外的广告领域规则。

## 任务
按"共同原因"把建议聚成 4-10 组（不超过 10 组），每组给出标题 + 一段 2-4 句运营叙事；
宁可多分一组，也不要把原因明显不同的建议揉进同一组。
剩下少数难以归组的特殊建议放进 `special_cases` 尾部清单。

## 分组依据（按优先级）
1. 同 action + 同 triggered_rule（典型共同原因，如 12 个广泛词都因 ACOS_UNRECOVERABLE 被淘汰）
2. 同 action + 同业务现象（如多个精准词都因 "TOS 广告位 ACOS 过高" 被下调）
3. 同 match_type + 同 keyword_class 的相同动作（精准/广泛策略一致）

## 输出 JSON（严格遵循 schema，禁 markdown 围栏）

{
  "groups": [
    {
      "title": "12 个广泛词因连续 7 天 ACOS 严重超标被降出价",
      "action": "adjust_bid",
      "common_reason_code": "ACOS_UNRECOVERABLE",
      "narrative": "这批广泛词 7 天 ACOS 普遍超 60%，订单稀少；为控制无效花费统一下调 Bid 约 30%，预算同步收紧。建议同步关注次周转化恢复情况，若 ACOS 仍居高需进一步处理。",
      "campaign_keys": ["活动A × B0AAA", "活动B × B0BBB"],
      "count": 12
    }
  ],
  "special_cases": [
    {"campaign_key": "活动X × B0XXX", "why_special": "唯一一个核心词被建议淘汰，需运营复核 Custom 保护名单"}
  ]
}

## 文案禁则
- narrative 用运营可读中文，禁规则编号（如 NO_CVR_HIGH_SPEND / ACOS_UNRECOVERABLE 等）
- 禁内部术语（"决策矩阵"/"决策包"/"门禁"/"confidence"）
- 引用数值要带时间窗口（如 "7 天花费 $X、订单 0"）
- 每组叙事 2-4 句，控制在 200 字内

## 严格要求
- 所有出现在 groups[*].campaign_keys 的值必须存在于输入数组的 campaign_key 字段中（禁止编造）
- 同一 campaign_key 只能出现在一组里（互斥分组），剩余的放 special_cases
- 输出纯 JSON，不含 markdown 代码块标记
"""


def _build_campaign_synthesis_prompt() -> str:
    # synthesis 只做语义分组 + 叙事，不做广告判断，不需要任何 KB（2026-06-08）
    # 删除 26K 的 campaign_adjustment KB → 降输入 token / 延迟 / 超时风险
    return _CAMPAIGN_SYNTHESIS_PROMPT


# ── Campaign 策略总览(执行总纲) Prompt ───────────────────────────────────────

_CAMPAIGN_OVERVIEW_PROMPT = """你是资深亚马逊广告策略分析师。基于知识库规则和给定的「策略上下文 + 当前状态数字」，为这个 ASIN 产出**今天广告调整的执行总纲**——宏观方向，不聚焦任何单个活动。

重要：所有输出使用中文，JSON key 用英文。

## 业务知识（必须遵循）
{kb_content}

## 硬性约束
- **只用用户消息给定的数字**，禁止编造或自行计算新数字（总预算、活动数、ACOS 分桶等都已给定）。
- **这是明细分析之前的总纲**：只描述"应该往哪个方向调"，**禁止**出现"已淘汰 N 个/已提价/预计预算下调$X"这类**分析结果**——那是后续汇总的事。
- 宏观视角，不要逐个活动点评。
- 广告方向只能用中文名：推进自然位 / 新增扩词 / 优化ACOS / 平衡维持。

## 输出格式（纯 JSON，不含 markdown 代码块标记）
{
  "status": "1.现状：用 2-3 句概括产品阶段/淡旺季/目标ACOS/总预算/库存评分退货是否健康/活动规模与当前ACOS分布。",
  "purpose": "2.调整目的：根据策略上下文推导今天的核心目标，并说明原因（如推进期叠旺季→以抢排名为主、兼顾效率，因自然单占比已高、广告依赖度低）。",
  "direction": "3.调整方向：整体执行打法——以哪个广告方向为主、哪个为辅，低效流量如何处理，排名词/核心词如何保护，并给原因。不含具体活动数。",
  "posture_brief": "一句到三句的精炼框架，供逐活动分析时作为统一判断基准（如：今日以平衡维持为主、优化ACOS为辅；保护排名型与核心词Bid不轻易下调；广泛词优先否词净化低效流量；库存/评分/退货正常，可适度收紧无效花费）。"
}
"""


def _build_campaign_overview_prompt() -> str:
    return _CAMPAIGN_OVERVIEW_PROMPT.replace("{kb_content}", kb.build("campaign_overview"))


class LLMReasoner:
    """LLM 推理器 —— 组装上下文并调用大模型"""

    def __init__(self, client: DeepSeekClient | None = None):
        # 默认复用全服务共享单例 deepseek_client（1 个连接池 + 服务级并发闸）。
        # 不再各自 new DeepSeekClient()，否则连接池碎片化、shutdown 漏关。
        self.client = client or deepseek_client

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

    _DIR_CN = {
        "push_natural": "推进自然位",
        "expand_keywords": "新增扩词",
        "optimize_acos": "优化ACOS",
        "balance_maintain": "平衡维持",
    }

    @classmethod
    def _dir_label(cls, direction: str) -> str:
        return cls._DIR_CN.get(direction, direction)

    @staticmethod
    def _strip_direction_score_lines(text: str) -> str:
        """删除整行方向评分类叙述，避免残留碎片破坏分段"""
        kept: list[str] = []
        score_line = re.compile(
            r"(?:平衡维持|新增扩词|优化\s*ACOS|推进自然位|推自然位).{0,20}?\d+\s*分",
            re.I,
        )
        for line in text.splitlines():
            s = line.strip()
            if not s:
                kept.append(line)
                continue
            if score_line.search(s) and (s.count("分") >= 2 or re.search(r"\d+\s*分\s*(?:最高|最低)", s)):
                continue
            if re.fullmatch(r"[。；,，\s]+", s):
                continue
            kept.append(line)
        return "\n".join(kept)

    @staticmethod
    def _sanitize_ops_text(text: str) -> str:
        """去掉规则编号、内部等级词等运营不应看到的内容（保留换行结构）"""
        if not text:
            return text
        t = humanize_ops_text(_RULE_ID_PATTERN.sub("", text))
        t = re.sub(
            r"\b(?:PN|KE|OA|BM|CROSS)-?\d+\s*"
            r"(?:确认|强制修正|强制优化|强制|建议优化|已确认|需处理|需关注)?\s*",
            "",
            t,
            flags=re.I,
        )
        t = _INTERNAL_LEVEL_PATTERN.sub("", t)
        t = re.sub(r"\[(?:confirmed|suggest_optimize|force_correct)\]\s*", "", t, flags=re.I)
        t = re.sub(r"规则校验|校验规则|门禁|eligible|ineligible", "", t, flags=re.I)
        t = LLMReasoner._strip_direction_score_lines(t)
        t = re.sub(
            r"(?:平衡维持|新增扩词|优化\s*ACOS|推进自然位|推自然位)[^\n。；]{0,16}?\d+\s*分[^。\n；]*",
            "",
            t,
            flags=re.I,
        )
        t = re.sub(r"\d+\s*分(?:最高|最低)?[，,、]?\s*", "", t)
        t = re.sub(r"[ \t]{2,}", " ", t)
        t = re.sub(r"；\s*；", "；", t)
        t = re.sub(r"\n{3,}", "\n\n", t)
        return humanize_ops_text(t.strip())  # strip_rule_jargon + 半窗/全窗

    @staticmethod
    def _extract_trend_highlights(trend_hint: str | None) -> dict:
        """从 daily_trend_text 解析 ACOS/CVR 趋势叙事，供区间句改写"""
        if not trend_hint:
            return {}
        highlights: dict[str, str] = {}
        summary_m = re.search(r"趋势摘要:\s*([^\n]+)", trend_hint)
        if summary_m:
            for part in re.split(r"[；;]", summary_m.group(1)):
                part = part.strip()
                if "ACOS" in part.upper() or "acos" in part.lower():
                    highlights["acos_narrative"] = part
                if "CVR" in part.upper():
                    highlights["cvr_narrative"] = part
        acos_m = re.search(
            r"近\d+日ACOS从[^\n；]+?变化至[^\n；%]+%",
            trend_hint,
            re.I,
        )
        if acos_m:
            highlights.setdefault("acos_narrative", acos_m.group(0))
        cvr_m = re.search(
            r"近\d+日CVR从[^\n；]+?变化至[^\n；%]+%",
            trend_hint,
            re.I,
        )
        if cvr_m:
            highlights.setdefault("cvr_narrative", cvr_m.group(0))
        return highlights

    @classmethod
    def _rewrite_static_metrics(cls, clause: str, highlights: dict) -> str:
        """将区间波动描述改写为日趋势叙事（若有 daily_trend）"""
        clause = clause.strip()
        if not clause:
            return clause

        acos_range_pat = (
            r"(近?\d*日?)ACOS在[\d.]+%?\s*[-~至到]\s*[\d.]+%?\s*之间(?:波动)?"
        )
        if re.search(acos_range_pat, clause, re.I):
            if highlights.get("acos_narrative"):
                clause = re.sub(
                    acos_range_pat,
                    highlights["acos_narrative"],
                    clause,
                    count=1,
                    flags=re.I,
                )
            else:
                m = re.search(
                    r"ACOS在([\d.]+)%?\s*[-~至到]\s*([\d.]+)%?",
                    clause,
                    re.I,
                )
                if m:
                    lo, hi = float(m.group(1)), float(m.group(2))
                    trend_word = "略收窄" if hi > lo else "略扩大"
                    repl = f"近几日ACOS在{lo}%-{hi}%区间波动{trend_word}"
                    clause = re.sub(acos_range_pat, repl, clause, count=1, flags=re.I)

        cvr_range_pat = r"CVR在[\d.]+%?\s*[-~至到]\s*[\d.]+%?\s*之间(?:波动)?"
        if re.search(cvr_range_pat, clause, re.I):
            if highlights.get("cvr_narrative"):
                clause = re.sub(
                    cvr_range_pat,
                    highlights["cvr_narrative"],
                    clause,
                    count=1,
                    flags=re.I,
                )

        if (
            re.search(r"ACOS在[\d.]+.*之间|CVR在[\d.]+.*之间", clause, re.I)
            and "判断" not in clause
            and not re.search(r"从.+?至|升至|降至|变化至", clause)
        ):
            parts = [highlights[k] for k in ("acos_narrative", "cvr_narrative") if highlights.get(k)]
            if parts:
                return "，".join(parts)
        return clause

    @classmethod
    def _remove_duplicate_metric_lines(cls, text: str, basis_lines: list[str]) -> str:
        """删除段外、与决策依据重复的裸区间指标行"""
        if "【建议】" not in text or not basis_lines:
            return text
        head, tail = text.split("【建议】", 1)
        basis_blob = " ".join(basis_lines)
        kept: list[str] = []
        for line in head.split("\n"):
            s = line.strip()
            if not s or s.startswith("【") or "判断" in s:
                kept.append(line)
                continue
            if re.search(r"ACOS在[\d.]+.*之间|CVR在[\d.]+.*之间", s, re.I):
                if len(s) < 100 and (
                    s in basis_blob
                    or any(s[:20] in bl for bl in basis_lines)
                ):
                    continue
            kept.append(line)
        return "\n".join(kept) + "【建议】" + tail

    @classmethod
    def _sort_direction_analyses(
        cls, analyses: list[dict], scores: list[dict]
    ) -> list[dict]:
        """报告区分方向块按适配度得分降序"""
        if not analyses or not scores:
            return analyses
        score_by_label: dict[str, float] = {}
        for s in scores:
            label = s.get("label") or cls._dir_label(s.get("id", ""))
            sc = float(s.get("suitability_score", 0))
            score_by_label[label] = sc
            if s.get("id"):
                score_by_label[s["id"]] = sc
        return sorted(
            analyses,
            key=lambda da: -score_by_label.get(da.get("direction", ""), 0),
        )

    @classmethod
    def _split_basis_clauses(cls, block: str) -> list[str]:
        """将决策依据段落拆成可独立判断的短句"""
        block = block.strip()
        if not block:
            return []
        if "根据" in block and "判断" in block:
            return [block]
        out: list[str] = []
        for sent in re.split(r"(?<=[。；])", block):
            sent = sent.strip().rstrip("。；")
            if not sent or re.fullmatch(r"[。；,，\s]+", sent):
                continue
            if len(sent) > 45 and "，" in sent:
                parts = re.split(r"，(?=有\d+个|同时有|且近)", sent)
                if len(parts) > 1:
                    out.extend(p.strip() for p in parts if p.strip())
                    continue
            out.append(sent)
        return out

    @classmethod
    def _to_judge_line(cls, clause: str, highlights: dict | None = None) -> str:
        """单句改写成「根据…，判断…」"""
        clause = cls._rewrite_static_metrics(clause, highlights or {})
        clause = clause.strip().rstrip("。；")
        if not clause:
            return ""
        if "根据" in clause and "判断" in clause:
            return clause if clause.startswith("根据") else f"根据{clause.lstrip('根据')}"
        body = clause.lstrip("根据").strip()
        if re.search(r"从.+?至|升至|降至|变化至", body):
            if re.search(r"(超\d+%|ACOS超|未收录|恶化)", body):
                return f"根据{body}，判断应优先优化ACOS或小批量试词"
            return f"根据{body}，判断效率趋势已明确，宜结合阶段安排投放节奏"
        if re.search(r"(未收录|高转化词)", body):
            return f"根据{body}，判断旺季末期宜小批量试扩词并设ACOS上限，避免大规模放量"
        if re.search(r"(超\d+%|ACOS超|零转化|恶化|超标)", body):
            return f"根据{body}，判断应优先否词或降价，将整体ACOS控制在30%以内"
        if re.search(r"(近\d+日|波动|升|降|由.+?至|升至|降至)", body):
            return f"根据{body}，判断效率边际波动但仍可控，宜以维持结构为主并盯住ACOS"
        if re.search(r"(收割|旺季末期|旺季)", body):
            if re.search(r"(自然位|第\d+名|前列|自然单)", body):
                return f"根据{body}，判断推自然位边际价值低，宜以平衡维持守住排名与利润"
            return f"根据{body}，判断处于收割/旺季末期，宜稳健运营、控制扩词规模"
        if re.search(r"(自然位|自然单|第\d+名|前列)", body):
            return f"根据{body}，判断排名与流量结构较稳，宜维持现有投放结构"
        return f"根据{body}，判断需纳入本期策略考量"

    @staticmethod
    def _compact_overall_analysis(text: str) -> str:
        """综合概览：去掉分段标题，合并为连贯状态摘要"""
        if not text or "【" not in text:
            return (text or "").strip()
        chunks: list[str] = []
        for hdr in ("【决策依据】", "【建议】", "【后续关注】", "【分析与建议】"):
            m = re.search(rf"{re.escape(hdr)}\s*(.*?)(?=\s*【|$)", text, re.S)
            if m and m.group(1).strip():
                chunks.append(re.sub(r"\s+", " ", m.group(1).strip()))
        if chunks:
            return "\n\n".join(chunks) if len(chunks) > 1 else chunks[0]
        return re.sub(r"【[^】]+】\s*", "", text).strip()

    @classmethod
    def _polish_advice_body(cls, body: str, highlights: dict) -> str:
        """润色【分析与建议】正文：判断句 trend 化，保留 • 列表"""
        body = cls._sanitize_ops_text(body)
        out: list[str] = []
        for block in re.split(r"\n+", body):
            block = block.strip()
            if not block:
                continue
            if block.startswith("•"):
                out.append(block)
                continue
            if re.search(r"(?:平衡维持|新增扩词|优化\s*ACOS|推进自然位).{0,12}?\d+\s*分", block):
                continue
            for clause in cls._split_basis_clauses(block):
                line = cls._to_judge_line(clause, highlights)
                if line:
                    out.append(line)
        return "\n".join(out) if out else body.strip()

    @classmethod
    def _polish_sectioned_text(cls, text: str, trend_hint: str | None = None) -> str:
        """三段式文案：决策依据趋势化 + 去评分 + 去重"""
        if not text:
            return cls._sanitize_ops_text(text)

        highlights = cls._extract_trend_highlights(trend_hint)

        if "【分析与建议】" in text:
            m = re.search(r"(【分析与建议】\s*)(.*?)(\s*【后续关注】)", text, re.S)
            if m:
                new_body = cls._polish_advice_body(m.group(2), highlights)
                tail = cls._sanitize_ops_text(text[m.start(3):])
                return text[: m.start(1)] + m.group(1) + new_body + "\n\n" + tail.lstrip()
            return cls._sanitize_ops_text(text)

        if "【决策依据】" not in text:
            return cls._sanitize_ops_text(text)

        m = re.search(r"(【决策依据】\s*)(.*?)(\s*【建议】)", text, re.S)
        if not m:
            return cls._sanitize_ops_text(text)

        new_basis = cls._polish_advice_body(m.group(2), highlights)
        tail = cls._sanitize_ops_text(text[m.start(3):])
        result = text[: m.start(1)] + m.group(1) + new_basis + "\n\n" + tail.lstrip()
        return cls._remove_duplicate_metric_lines(result, new_basis.split("\n"))

    @classmethod
    def _polish_direction_analysis(cls, text: str, trend_hint: str | None = None) -> str:
        """分方向报告：合并【决策依据】+【建议】为【分析与建议】后 polish"""
        if not text:
            return text
        text = cls._sanitize_ops_text(text)
        if "【分析与建议】" not in text:
            basis_m = re.search(
                r"【决策依据】\s*(.*?)(?=\s*【建议】|\s*【后续关注】|$)", text, re.S
            )
            sugg_m = re.search(r"【建议】\s*(.*?)(?=\s*【后续关注】|$)", text, re.S)
            follow_m = re.search(r"【后续关注】\s*(.*)", text, re.S)
            merged: list[str] = []
            if basis_m and basis_m.group(1).strip():
                merged.append(basis_m.group(1).strip())
            if sugg_m and sugg_m.group(1).strip():
                merged.append(sugg_m.group(1).strip())
            follow = follow_m.group(1).strip() if follow_m else ""
            body = "\n".join(merged)
            text = f"【分析与建议】\n{body}"
            if follow:
                text += f"\n\n【后续关注】\n{follow}"
        return cls._polish_sectioned_text(text, trend_hint)

    @classmethod
    def _polish_exec_reasoning(cls, text: str, trend_hint: str | None = None) -> str:
        return cls._polish_sectioned_text(text, trend_hint)

    @classmethod
    def _sanitize_analysis(cls, result: dict) -> dict:
        if result.get("overall_analysis"):
            result["overall_analysis"] = cls._sanitize_ops_text(result["overall_analysis"])
        if result.get("skip_directions_note"):
            result["skip_directions_note"] = cls._sanitize_ops_text(result["skip_directions_note"])
        for da in result.get("direction_analyses") or []:
            if da.get("analysis"):
                da["analysis"] = cls._sanitize_ops_text(da["analysis"])
            if da.get("reasoning"):
                da["reasoning"] = cls._sanitize_ops_text(da["reasoning"])
        return result

    @staticmethod
    def _prepend_missing_notice(context: str, missing_notice: str | None) -> str:
        if not missing_notice or not missing_notice.strip():
            return context
        return f"## 数据完整性说明\n{missing_notice.strip()}\n\n{context}"

    @staticmethod
    def _merge_suggestions_into_analysis(analysis: str, suggestions: list) -> str:
        if not suggestions:
            return analysis
        bullets = "\n".join(f"• {s}" for s in suggestions if s)
        if not bullets:
            return analysis
        if "【建议】" in analysis:
            return f"{analysis.rstrip()}\n{bullets}"
        if analysis.strip():
            return f"{analysis.rstrip()}\n\n【建议】\n{bullets}"
        return f"【建议】\n{bullets}"

    @classmethod
    def _normalize_analysis_output(cls, result: dict) -> dict:
        """合并旧版 suggestions 字段，统一方向中文名"""
        for da in result.get("direction_analyses") or []:
            raw_dir = da.get("direction") or ""
            da["direction"] = cls._DIR_CN.get(raw_dir, raw_dir)
            body = da.get("analysis") or da.get("reasoning") or ""
            da["analysis"] = cls._merge_suggestions_into_analysis(
                body, da.get("suggestions") or []
            )
        return result

    def _build_context(self, data_summary: dict, scores: list[dict],
                       validations: dict, decisions: dict,
                       asin: str,
                       strategy: dict | None = None,
                       tactics: dict | None = None,
                       days: int = 7,
                       selected_directions: list[str] | None = None,
                       eligible_directions: list[str] | None = None) -> str:
        """将结构化数据组装为 LLM 可读的上下文文本"""
        parts = []
        dir_labels = {
            "push_natural": "推进自然位",
            "expand_keywords": "新增扩词",
            "optimize_acos": "优化ACOS",
            "balance_maintain": "平衡维持",
        }

        if selected_directions is not None:
            labels = [dir_labels.get(d, d) for d in selected_directions]
            parts.append("## 运营已选方向（direction_analyses 仅分析这些）")
            parts.append(f"  {labels}")
            parts.append("")
        if eligible_directions is not None:
            parts.append("## 规则可推荐方向（评分≥40）")
            parts.append(f"  {[dir_labels.get(d, d) for d in eligible_directions]}")
            parts.append("")

        # 结构化字段列表（不进入基础信息循环）
        STRUCTURED_KEYS = {
            "high_acos_keywords", "rising_keywords", "wasteful_keywords",
            "top_cvr_keywords", "keyword_trend_watch", "daily_trend", "daily_trend_text",
            "expand_keyword_candidates", "competitor_summary", "placement_comparison",
            "data_completeness", "analysis_days",
        }

        # 战略层上下文
        if strategy:
            parts.append("## 战略层（人工选择）")
            parts.append(f"  - 产品定位: {product_level_with_code(strategy.get('product_level', '')) or '?'}")
            parts.append(f"  - 产品阶段: {strategy.get('product_stage', '?')}")
            parts.append(f"  - 淡旺季: {strategy.get('season_stage', '?')}")
            parts.append("")

        # 策略层上下文
        if tactics:
            parts.append("## 策略层（人工选择）")
            parts.append(f"  - 广告目的: {tactics.get('ad_purposes', [])}")
            parts.append(f"  - 关键词类型: {tactics.get('target_keyword_strategy', [])}")
            parts.append("")

        # ASIN 基本信息（仅标量字段）
        parts.append(f"## ASIN 基本信息（数据窗口: {days}天）")
        if data_summary:
            lines = []
            for k, v in data_summary.items():
                if k in STRUCTURED_KEYS:
                    continue
                lines.append(f"  - {k}: {v}")
            parts.append("\n".join(lines))

        # ── ASIN 日趋势 ──
        if data_summary.get("daily_trend_text"):
            parts.append(f"\n## ASIN 日趋势（近{days}天，分析时请优先看趋势方向）")
            parts.append(data_summary["daily_trend_text"])

        # ── 关键词级洞察（含时间窗对比）──
        def _kw_trend_line(kw: dict) -> str:
            seg = [f"[{kw.get('match_type', '')}] {kw.get('keyword', '?')}"]
            _, _, window_label, _, _ = period_labels(days)
            spend_key = f"spend_{days}d"
            if kw.get(spend_key) is not None:
                seg.append(f"{window_label}花费 ${kw[spend_key]}")
            half = max(1, days // 2)
            ap = kw.get(f"acos前{days - half}日")
            ar = kw.get(f"acos近{half}日")
            acos_line = format_keyword_acos_change(
                days, kw.get("acos"), ap, ar, kw.get("acos_trend")
            )
            if acos_line:
                seg.append(acos_line)
            elif kw.get("acos") is not None:
                seg.append(f"{window_label}整体ACOS {kw['acos']}%")
            if kw.get("rank_trend"):
                rc = kw.get("rank_change_14d")
                nr = kw.get("natural_rank")
                seg.append(f"{kw['rank_trend']}" + (f"，14日升{rc}位，现第{nr}名" if rc and nr else ""))
            if kw.get("orders") is not None:
                seg.append(f"订单 {kw['orders']}单")
            return "  - " + "；".join(seg)

        if data_summary.get("keyword_trend_watch"):
            parts.append("\n## 趋势异动词 TOP5（优先结合趋势判断）")
            for kw in data_summary["keyword_trend_watch"]:
                parts.append(_kw_trend_line(kw))

        if data_summary.get("high_acos_keywords"):
            parts.append("\n## 高ACOS关键词 TOP5")
            for kw in data_summary["high_acos_keywords"]:
                parts.append(_kw_trend_line(kw))

        if data_summary.get("rising_keywords"):
            parts.append("\n## 上升关键词 TOP5")
            for kw in data_summary["rising_keywords"]:
                parts.append(_kw_trend_line(kw))

        if data_summary.get("wasteful_keywords"):
            parts.append("\n## 高花费零转化词")
            for kw in data_summary["wasteful_keywords"]:
                parts.append(_kw_trend_line(kw))

        if data_summary.get("top_cvr_keywords"):
            parts.append("\n## 高转化在投词 TOP10")
            for kw in data_summary["top_cvr_keywords"]:
                parts.append(_kw_trend_line(kw))

        expand_cands = data_summary.get("expand_keyword_candidates") or []
        if expand_cands:
            parts.append(
                f"\n## 待扩词候选（高转化未收录，共 {len(expand_cands)} 个；"
                "≤10 个时须在输出中**全部列出**英文词，禁止用「等」省略）"
            )
            for item in expand_cands:
                if isinstance(item, dict):
                    kw = item.get("keyword", "?")
                    cr = item.get("convert_ratio")
                    sr = item.get("searches")
                    extra = []
                    if cr is not None:
                        extra.append(f"转化比{cr}")
                    if sr is not None:
                        extra.append(f"搜索量{sr}")
                    suffix = f" ({', '.join(extra)})" if extra else ""
                    parts.append(f"  - 「{kw}」{suffix}")
                else:
                    parts.append(f"  - 「{item}」")

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

        # 数据结论（供内部分析，输出禁止引用编号）
        parts.append("\n## 数据结论参考（已转为运营语言，引用时勿写 PN/BM/OA 等编号）")
        val_dirs = selected_directions if selected_directions else list(validations.keys())
        for direction in val_dirs:
            result = validations.get(direction, {})
            items = (result or {}).get("items", []) or []
            if not items:
                continue
            parts.append(f"\n### {dir_labels.get(direction, direction)}")
            for item in items:
                msg = item.get("display_message") or format_validation_item_ops(item)
                parts.append(f"  - {msg}")

        # 决策任务
        parts.append("\n## 决策执行任务（LLM suggestions 应在此基础上补充，勿矛盾）")
        dec_dirs = selected_directions if selected_directions else list(decisions.keys())
        for direction in dec_dirs:
            result = decisions.get(direction, {})
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

    async def recommend_execution(
        self,
        asin: str,
        data_summary: dict,
        strategy: dict,
        tactics: dict,
        scores: list[dict],
        eligible_directions: list[str] | None = None,
        ineligible_directions: list[dict] | None = None,
        missing_notice: str | None = None,
    ) -> dict:
        """基于全上下文推荐执行方向（仅在 eligible 范围内）"""
        min_score = 40
        if eligible_directions is None:
            eligible_directions = [
                s["id"] for s in scores
                if s.get("suitability_score", 0) >= min_score
            ]
        if ineligible_directions is None:
            ineligible_directions = [
                {"id": s["id"], "label": s.get("label", s["id"]),
                 "score": s.get("suitability_score"), "reason": s.get("reason", "")}
                for s in scores
                if s.get("id") not in eligible_directions
            ]

        context_parts = [
            "## 战略层",
            f"  - 产品定位: {product_level_with_code(strategy.get('product_level', '')) or '?'}",
            f"  - 产品阶段: {strategy.get('product_stage', '?')}",
            f"  - 淡旺季: {strategy.get('season_stage', '?')}",
            "",
            "## 策略层",
            f"  - 广告目的: {tactics.get('ad_purposes', [])}",
            f"  - 关键词类型: {tactics.get('target_keyword_strategy', [])}",
            "",
            "## 可推荐方向（eligible，仅可从中选择）",
            f"  {eligible_directions}",
            "",
            "## 禁止推荐方向（ineligible）",
        ]
        for item in ineligible_directions:
            context_parts.append(
                f"  - {item.get('label', item.get('id'))}: "
                f"{item.get('score')}分 — {item.get('reason', '')}"
            )
        context_parts.extend([
            "",
            "## 方向评分（内部排序用，reasoning 正文禁止写「82分」等，须用趋势+判断说明优先级）",
        ])
        for s in scores:
            context_parts.append(
                f"  - {s.get('label', s.get('id', '?'))}: "
                f"{s.get('suitability_score', '?')}分 ({s.get('suitability', '?')}) — {s.get('reason', '')}"
            )

        win_days = int(data_summary.get("analysis_days") or 7)
        if data_summary.get("daily_trend_text"):
            context_parts.extend([
                "",
                f"## ASIN 日趋势（近{win_days}天，reasoning 的【决策依据】须优先引用趋势）",
                data_summary["daily_trend_text"],
            ])
        watch = data_summary.get("keyword_trend_watch") or []
        if watch:
            context_parts.append("\n## 关键词趋势异动 TOP（须结合 acos_trend/rank_trend 判断）")
            half = max(1, win_days // 2)
            for kw in watch[:5]:
                line = f"  - {kw.get('keyword')}: {kw.get('rank_trend', '')}"
                ap = kw.get(f"acos前{win_days - half}日")
                ar = kw.get(f"acos近{half}日")
                acos_line = format_keyword_acos_change(
                    win_days, kw.get("acos"), ap, ar, kw.get("acos_trend")
                )
                if acos_line:
                    line += f", {acos_line}"
                context_parts.append(line)

        user_body = self._prepend_missing_notice(
            "\n".join(context_parts),
            missing_notice or data_summary.get("missing_notice"),
        )
        messages = [
            {"role": "system", "content": _build_execution_system_prompt()},
            {"role": "user", "content": (
                f"请为 ASIN ({asin}) 推荐广告执行方向。\n\n{user_body}"
            )},
        ]

        try:
            raw = await self.client.chat(
                messages=messages,
                temperature=0.3,
                response_format={"type": "json_object"},
            )
            parsed = self._parse_json(raw)
            if parsed.get("reasoning"):
                parsed["reasoning"] = humanize_ops_text(
                    self._polish_sectioned_text(
                        parsed["reasoning"],
                        (data_summary or {}).get("daily_trend_text"),
                    )
                )
            if parsed.get("conflict_notes"):
                parsed["conflict_notes"] = self._sanitize_ops_text(parsed["conflict_notes"])
            if parsed.get("reasoning") and "【决策依据】" not in parsed["reasoning"]:
                labels = [
                    self._dir_label(d) for d in parsed.get("recommended_directions", [])
                ]
                parsed["reasoning"] = (
                    f"【决策依据】基于适配度评分与产品阶段，可推荐方向包括："
                    f"{'、'.join(labels) or '见卡片评分'}。\n\n"
                    f"【建议】{parsed['reasoning']}\n\n"
                    "【后续关注】生成完整报告后结合校验规则复核。"
                )
            return parsed
        except Exception as e:
            logger.warning("LLM 执行推荐失败，使用评分降级: %s", e)
            top = sorted(scores, key=lambda s: s.get("suitability_score", 0), reverse=True)
            recommended = [s["id"] for s in top if s["id"] in eligible_directions][:2]
            labels = [self._dir_label(d) for d in recommended]
            return {
                "recommended_directions": recommended or (
                    eligible_directions[:1] if eligible_directions else ["balance_maintain"]
                ),
                "reasoning": (
                    "【决策依据】LLM 暂不可用，已按规则评分在可推荐方向内排序。\n\n"
                    f"【建议】优先考虑：{'、'.join(labels) or '平衡维持'}。\n\n"
                    "【后续关注】服务恢复后可重新加载方向推荐以获取完整分析。"
                ),
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
        days: int = 7,
        trend_text: str = "",
        missing_notice: str | None = None,
    ) -> dict:
        """P3 统一推荐：LLM 同时给出目标 ACOS 和预算/Bid 建议"""
        parts = [
            "## 战略层",
            f"  - 产品定位: {product_level_with_code(strategy.get('product_level', '')) or '?'}",
            f"  - 产品阶段: {strategy.get('product_stage', '?')}",
            f"  - 淡旺季: {strategy.get('season_stage', '?')}",
            "",
            "## 策略层",
            f"  - 广告目的: {tactics.get('ad_purposes', [])}",
            f"  - 关键词类型: {tactics.get('target_keyword_strategy', [])}",
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
            f"## 诊断数据（数据窗口: {days}天）",
        ])
        for k, v in data_summary.items():
            if v is not None:
                parts.append(f"  - {k}: {v}")
        parts.append("")

        if trend_text:
            parts.append(f"## 每日趋势数据（近{days}天，逐日变化）")
            parts.append(trend_text)
            parts.append("")

        if keyword_details:
            parts.append(f"## 关键词级数据（Top关键词，以下 spend 均为{days}天总计）")
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

        user_body = self._prepend_missing_notice(
            "\n".join(parts),
            missing_notice or data_summary.get("missing_notice"),
        )
        messages = [
            {"role": "system", "content": _build_p3_system_prompt()},
            {"role": "user", "content": (
                f"请为 ASIN ({asin}) 同时给出目标ACOS和预算/Bid推荐。\n\n{user_body}"
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
        days: int = 7,
        selected_directions: list[str] | None = None,
        eligible_directions: list[str] | None = None,
        missing_notice: str | None = None,
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
            days=days,
            selected_directions=selected_directions,
            eligible_directions=eligible_directions,
        )

        context_body = self._prepend_missing_notice(
            context,
            missing_notice or data_summary.get("missing_notice"),
        )
        messages = [
            {"role": "system", "content": _build_analyze_system_prompt()},
            {"role": "user", "content": (
                f"请分析以下 ASIN ({asin}) 的广告数据，生成综合分析报告。\n\n"
                f"{context_body}"
            )},
        ]

        try:
            raw = await self.client.chat(
                messages=messages,
                temperature=0.3,
                response_format={"type": "json_object"},
            )
            result = self._sanitize_analysis(
                self._normalize_analysis_output(self._parse_json(raw))
            )
        except Exception as e:
            logger.warning("LLM 分析失败，返回降级结果: %s", e)
            result = {
                "overall_analysis": (
                    f"【决策依据】LLM 分析暂不可用（{e}）。\n\n"
                    "【建议】请稍后重试生成报告。\n\n"
                    "【后续关注】服务恢复后重新生成评估报告。"
                ),
                "direction_analyses": [],
                "action_priorities": [],
                "risk_warnings": [],
            }

        result.setdefault("direction_analyses", [])
        result.setdefault("action_priorities", [])
        result.setdefault("risk_warnings", [])
        result.setdefault("skip_directions_note", "")

        if selected_directions:
            allowed_labels = {
                self._dir_label(d) for d in selected_directions
            } | set(selected_directions)
            result["direction_analyses"] = [
                da for da in result.get("direction_analyses", [])
                if da.get("direction") in allowed_labels
            ]

        trend_hint = (data_summary or {}).get("daily_trend_text")
        if result.get("overall_analysis"):
            raw_overall = self._sanitize_ops_text(result["overall_analysis"])
            result["overall_analysis"] = humanize_ops_text(
                self._compact_overall_analysis(raw_overall)
            )
        for da in result.get("direction_analyses") or []:
            body = da.get("analysis") or da.get("reasoning") or ""
            if body:
                da["analysis"] = humanize_ops_text(
                    self._polish_direction_analysis(body, trend_hint)
                )
        result["direction_analyses"] = self._sort_direction_analyses(
            result.get("direction_analyses") or [], scores or []
        )
        if result.get("skip_directions_note"):
            result["skip_directions_note"] = humanize_ops_text(
                self._sanitize_ops_text(result["skip_directions_note"])
            )
        return result

    # ── Campaign 活动调整 ──────────────────────────────────────────────────

    @staticmethod
    def _campaign_to_prompt_dict(
        cu: CampaignUnit,
        target_acos: int | None = None,
        keyword_class: str = "",
        is_core: bool = False,
    ) -> dict:
        """CampaignUnit → LLM prompt dict。

        keyword_class: 逐词 AI 分类 (取自 keyword_analysis, 已转中文)
        is_core: KB 21 Custom 核心词保护
        """
        p = cu.perf_7d
        campaign_type = "精准广告" if cu.match_type == "EXACT" else "广泛广告"

        summary: dict = {
            "campaign_name": cu.campaign_name,
            "campaign_key": cu.campaign_key,
            "child_asin": cu.child_asin,
            "keyword_text": cu.keyword_text,
            "match_type": cu.match_type,
            "campaign_type": campaign_type,
            "current_bid": round(cu.current_bid, 4),
            "current_budget": round(cu.current_budget, 2),
            "campaign_status": cu.campaign_status,
            "days_online": cu.days_online,
            "days_online_note": "未知（非新活动）" if cu.days_online == -1 else f"{cu.days_online}天",
            "perf_7d": {
                "cost": round(p.cost, 2),
                "sales": round(p.sales, 2),
                "orders": p.orders,
                "acos": round(p.acos, 1) if p.acos is not None else None,
                "cvr": round(p.cvr, 1) if p.cvr is not None else None,
                "cpc": round(p.cpc, 2) if p.cpc is not None else None,
                "ctr": round(p.ctr, 1) if p.ctr is not None else None,
                "clicks": p.clicks,
                "impressions": p.impressions,
            },
        }

        if target_acos is not None and p.acos is not None:
            summary["acos_vs_target"] = round(p.acos - target_acos, 1)
        if cu.current_budget > 0 and p.cost > 0:
            summary["budget_utilization_pct"] = round(p.cost / (cu.current_budget * 7) * 100, 1)
        if keyword_class:
            summary["keyword_class"] = keyword_class
        if is_core:
            summary["is_core"] = True

        return summary

    async def recommend_campaign_batch(
        self,
        asin: str,
        campaign_summaries: list[dict],
        strategy_context: dict,
        temperature: float = 0.3,
        *,
        task_type: str = "exact",
        timeout_override: float | None = None,
    ) -> dict:
        """分析单批活动 (≤6个) 并返回调整建议。

        task_type: "exact" → 精准专用 prompt/schema; "broad" → 广泛专用 prompt/schema
        timeout_override: 单次 HTTP socket 超时 (秒); 不传走 client 默认值。
            纵深防御: 与 sanity/synthesis 一致,httpx socket 层自断绕过 asyncio 取消缺陷。

        Returns:
            {"parsed": dict, "raw_output": str, "success": bool, "error": str, "temperature": float}
        """
        logger.info(
            "Campaign batch LLM 入口 [%s] task=%s items=%d temp=%.2f",
            asin, task_type, len(campaign_summaries), temperature,
        )
        # 构建策略上下文字符串
        ctx_parts = [
            "## 策略上下文 (ASIN 级，全批次共享)",
            f"  - 产品阶段: {strategy_context.get('product_stage', '?')}",
            f"  - 产品定位: {product_level_with_code(strategy_context.get('product_level', '')) or '?'}",
            f"  - 淡旺季: {strategy_context.get('season_stage', '?')}",
            f"  - 广告目的: {strategy_context.get('ad_purposes', [])}",
            f"  - 目标关键词类型: {strategy_context.get('target_keyword_strategy', [])}",
        ]
        margin = strategy_context.get("margin")
        ctx_parts.append(f"  - 毛利率: {'%.1f%%' % (margin * 100) if margin is not None else 'N/A'}")
        ctx_parts.append(f"  - 评分: {strategy_context.get('rating', 'N/A')}")
        ctx_parts.append(f"  - 退货率: {'%.1f%%' % strategy_context['refund_rate'] if strategy_context.get('refund_rate') is not None else 'N/A'}")
        inv_days = strategy_context.get("inventory_days")
        ctx_parts.append(f"  - 库存可售天数: {'%.0f天' % inv_days if inv_days is not None else 'N/A'}")
        ctx_parts.append(f"  - 自然单占比: {'%.1f%%' % strategy_context['natural_order_ratio'] if strategy_context.get('natural_order_ratio') is not None else 'N/A'}")
        ctx_parts.append(f"  - 日均销量(30d): {'%.1f单' % strategy_context['avg_daily_sales_30d'] if strategy_context.get('avg_daily_sales_30d') is not None else 'N/A'}")
        if strategy_context.get("target_acos"):
            ctx_parts.append(f"  - 运营目标 ACOS: {strategy_context['target_acos']}%")
        _db = strategy_context.get("daily_budget")
        if _db is not None:
            ctx_parts.append(f"  - 运营每日预算基准: ${_db:.2f}/天")
        _adirs = strategy_context.get("ad_directions") or []
        if _adirs:
            ctx_parts.append(f"  - 广告方向(运营已选): {_adirs}")
        flags = strategy_context.get("warning_flags", [])
        if flags:
            ctx_parts.append(f"  - ⚠️ 注意事项: {'; '.join(flags)}")
        ctx_parts.append("")

        # 构建活动列表
        camp_parts = ["## 活动列表 (逐活动分析)"]
        for i, s in enumerate(campaign_summaries):
            camp_parts.append(f"\n### 活动 {i + 1}: {s.get('campaign_name', '?')}")
            camp_parts.append(f"  - 活动Key (活动名×子ASIN): {s.get('campaign_key', '')}")
            camp_parts.append(f"  - 子ASIN: {s.get('child_asin', '')}")
            camp_parts.append(f"  - 关键词: {s.get('keyword_text', '')}")
            camp_parts.append(f"  - 匹配类型: {s.get('match_type', '')}")
            camp_parts.append(f"  - 活动类型: {s.get('campaign_type', '')}")
            camp_parts.append(f"  - 当前 Bid: ${s.get('current_bid', 0)}")
            camp_parts.append(f"  - 当前 Budget: ${s.get('current_budget', 0)}")
            camp_parts.append(f"  - 状态: {s.get('campaign_status', '')}")
            camp_parts.append(f"  - 上线天数: {s.get('days_online_note', '?')}")
            if s.get("keyword_class"):
                camp_parts.append(f"  - 关键词类型: {s['keyword_class']}")
            if s.get("is_core"):
                camp_parts.append(f"  - ⚠️ 核心词 (Custom保护)")
            p = s.get("perf_7d", {})
            camp_parts.append(f"  - 7日性能: 花费${p.get('cost', 0)}, 销售额${p.get('sales', 0)}, "
                             f"订单{p.get('orders', 0)}, ACOS={p.get('acos', 'N/A')}%, "
                             f"CVR={p.get('cvr', 'N/A')}%, CPC=${p.get('cpc', 'N/A')}, "
                             f"CTR={p.get('ctr', 'N/A')}%, "
                             f"曝光{p.get('impressions', 0)}, 点击{p.get('clicks', 0)}")
            if "acos_vs_target" in s:
                camp_parts.append(f"  - ACOS vs 目标: {'+' if s['acos_vs_target'] > 0 else ''}{s['acos_vs_target']}%")
            if "budget_utilization_pct" in s:
                camp_parts.append(f"  - 预算利用率: {s['budget_utilization_pct']}%")
            # 广告位懒加载数据 (KB 22 §2.3 / KB 19 §5)
            if s.get("_placement_data"):
                pd_data = s["_placement_data"]
                camp_parts.append(f"  - ★广告位数据 (per-placement):")
                for pname, pinfo in pd_data.items():
                    if isinstance(pinfo, dict):
                        camp_parts.append(
                            f"      {pname}: ACOS={pinfo.get('acos','N/A')}%, "
                            f"花费=${pinfo.get('cost',0)}, 订单={pinfo.get('orders',0)}, "
                            f"点击={pinfo.get('clicks',0)}, 曝光={pinfo.get('impressions',0)}"
                        )
            # 搜索词懒加载数据 (KB 22 §3.3 / KB 19 §8)
            if s.get("_search_term_data"):
                st_data = s["_search_term_data"]
                terms = st_data if isinstance(st_data, list) else st_data.get("search_terms", [])
                if terms:
                    camp_parts.append(f"  - ★搜索词报告 ({len(terms)} 个搜索词):")
                    for t in terms[:15]:  # 最多展示15个
                        if isinstance(t, dict):
                            camp_parts.append(
                                f"      [{t.get('keyword','?')}] 花费=${t.get('cost',0)}, "
                                f"订单={t.get('orders',0)}, 点击={t.get('clicks',0)}, "
                                f"ACOS={t.get('acos','N/A')}%"
                            )

        # 策略总览(执行总纲)preamble：非空时置于用户消息最前，作为本批逐活动判断的统一框架
        overview_text = (strategy_context.get("_strategic_overview_text") or "").strip()
        preamble = (
            f"## 今日执行总纲（逐活动判断须遵循此宏观框架）\n{overview_text}\n\n---\n\n"
            if overview_text else ""
        )

        user_message = preamble + "\n".join(ctx_parts) + "\n" + "\n".join(camp_parts)

        messages = [
            {"role": "system", "content": _build_campaign_system_prompt(task_type)},
            {"role": "user", "content": user_message},
        ]

        try:
            raw = await self.client.chat(
                messages=messages,
                temperature=temperature,
                response_format={"type": "json_object"},
                max_tokens=8192,
                timeout_override=timeout_override,
            )
            parsed = self._parse_json(raw)
            adjustments = parsed.get("campaign_adjustments", [])
            # 后处理：规则编号脱敏 + 文风清洗
            for adj in adjustments:
                adj["reason"] = humanize_ops_text(
                    self._sanitize_ops_text(adj.get("reason", ""))
                )
                adj["evidence"] = [
                    humanize_ops_text(self._sanitize_ops_text(e))
                    for e in adj.get("evidence", [])
                ]
            parsed["campaign_adjustments"] = adjustments
            logger.info("Campaign batch LLM 成功 [%s], %d items, temp=%.1f",
                        asin, len(adjustments), temperature)
            return {
                "parsed": parsed,
                "raw_output": raw,
                "success": True,
                "error": "",
                "temperature": temperature,
            }
        except Exception as e:
            logger.warning("Campaign batch LLM 失败 [%s] temp=%.1f: %s", asin, temperature, e)
            return {
                "parsed": {},
                "raw_output": "",
                "success": False,
                "error": str(e),
                "temperature": temperature,
            }

    # ── Campaign 策略总览(执行总纲) ──────────────────────────────────────────
    async def recommend_campaign_overview(
        self,
        asin: str,
        facts: dict,
        strategy_context: dict,
        *,
        temperature: float = 0.3,
        timeout_override: float | None = None,
    ) -> dict:
        """基于策略上下文 + 当前状态数字，产出执行总纲三段 + posture_brief。

        数字全部由调用方(Python)算好放进 facts，本方法只做叙事。
        失败返回 {"error": "..."}；调用方据此走 fail-open。
        """
        logger.info("Campaign overview LLM 入口 [%s] facts_keys=%d", asin, len(facts))

        ctx_lines = [
            "## 策略上下文",
            f"  - 产品阶段: {strategy_context.get('product_stage', '?')}",
            f"  - 产品定位: {product_level_with_code(strategy_context.get('product_level', '')) or '?'}",
            f"  - 淡旺季: {strategy_context.get('season_stage', '?')}",
            f"  - 广告目的: {strategy_context.get('ad_purposes', [])}",
            f"  - 广告方向(运营已选): {strategy_context.get('ad_directions', []) or '未选'}",
            f"  - 目标关键词类型: {strategy_context.get('target_keyword_strategy', [])}",
        ]
        margin = strategy_context.get("margin")
        ctx_lines.append(f"  - 毛利率: {'%.1f%%' % (margin * 100) if margin is not None else 'N/A'}")
        db = strategy_context.get("daily_budget")
        ctx_lines.append(f"  - 运营每日预算基准: {'$%.2f/天' % db if db is not None else 'N/A'}")
        ctx_lines.append(f"  - 评分: {strategy_context.get('rating', 'N/A')}")
        ctx_lines.append(f"  - 退货率: {'%.1f%%' % strategy_context['refund_rate'] if strategy_context.get('refund_rate') is not None else 'N/A'}")
        inv = strategy_context.get("inventory_days")
        ctx_lines.append(f"  - 库存可售天数: {'%.0f天' % inv if inv is not None else 'N/A'}")
        ctx_lines.append(f"  - 自然单占比: {'%.1f%%' % strategy_context['natural_order_ratio'] if strategy_context.get('natural_order_ratio') is not None else 'N/A'}")
        if strategy_context.get("target_acos"):
            ctx_lines.append(f"  - 运营目标 ACOS: {strategy_context['target_acos']}%")
        flags = strategy_context.get("warning_flags", []) or []
        if flags:
            ctx_lines.append(f"  - ⚠️ 注意事项: {'; '.join(flags)}")

        user_message = (
            "\n".join(ctx_lines)
            + "\n\n## 当前状态数字（仅可引用，禁止改算）\n"
            + json.dumps(facts, ensure_ascii=False)
        )

        messages = [
            {"role": "system", "content": _build_campaign_overview_prompt()},
            {"role": "user", "content": user_message},
        ]

        try:
            raw = await self.client.chat(
                messages=messages,
                temperature=temperature,
                response_format={"type": "json_object"},
                max_tokens=2048,
                timeout_override=timeout_override or 55,
            )
            parsed = self._parse_json(raw)
            out = {
                "status_text": self._sanitize_ops_text(parsed.get("status", "") or ""),
                "purpose_text": self._sanitize_ops_text(parsed.get("purpose", "") or ""),
                "direction_text": self._sanitize_ops_text(parsed.get("direction", "") or ""),
                "posture_brief": self._sanitize_ops_text(parsed.get("posture_brief", "") or ""),
            }
            logger.info("Campaign overview 成功 [%s]", asin)
            return out
        except Exception as e:
            logger.warning("Campaign overview 失败 [%s]: %s", asin, e)
            return {"error": str(e)}

    # ── Campaign 汇总合成 ──────────────────────────────────────────────────
    async def recommend_campaign_synthesis(
        self,
        asin: str,
        adjustments: list,
        strategy_context: dict,
        *,
        temperature: float = 0.3,
        timeout_override: float | None = None,
    ) -> dict:
        """把 N 条单活动建议合成为 4-7 段按共同原因分组的运营叙事 + 特殊调整尾部清单。

        失败返回 {"groups": [], "special_cases": [], "error": "..."}。调用方自行判断。
        """
        if not adjustments:
            return {"groups": [], "special_cases": []}

        logger.info(
            "Campaign synthesis LLM 入口 [%s] adjustments=%d timeout=%ss",
            asin, len(adjustments), timeout_override or 55,
        )

        # 精简单条信息，控制输入 token
        compact: list[dict] = []
        for adj in adjustments:
            d = adj.model_dump() if hasattr(adj, "model_dump") else dict(adj)
            compact.append({
                "campaign_key": d.get("campaign_key", ""),
                "campaign_name": (d.get("campaign_name") or "")[:60],
                "action": d.get("action", ""),
                "triggered_rule": d.get("triggered_rule", ""),
                "match_type": d.get("match_type", ""),
                "keyword_class": d.get("keyword_class", ""),
                "is_core": d.get("is_core", False),
                "current_bid": d.get("current_bid"),
                "proposed_bid": d.get("proposed_bid"),
                "current_budget": d.get("current_budget"),
                "proposed_budget": d.get("proposed_budget"),
                "reason": d.get("reason") or "",  # 完整理由：分组命脉，不截断
                "confidence": d.get("confidence", "medium"),
            })

        ctx_lines = [
            f"  - 产品阶段: {strategy_context.get('product_stage', '?')}",
            f"  - 广告目的: {strategy_context.get('ad_purposes', [])}",
            f"  - 目标 ACOS: {strategy_context.get('target_acos', '?')}%",
        ]
        warning_flags = strategy_context.get("warning_flags", []) or []
        if warning_flags:
            ctx_lines.append(f"  - ⚠️ 注意事项: {'; '.join(warning_flags)}")

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

        try:
            raw = await self.client.chat(
                messages=messages,
                temperature=temperature,
                response_format={"type": "json_object"},
                max_tokens=8192,
                timeout_override=timeout_override or 55,
            )
            parsed = self._parse_json(raw)
            groups = parsed.get("groups", []) or []
            special_cases = parsed.get("special_cases", []) or []
            # 文风脱敏 narrative
            for g in groups:
                if g.get("narrative"):
                    g["narrative"] = humanize_ops_text(
                        self._sanitize_ops_text(g["narrative"])
                    )
                if g.get("title"):
                    g["title"] = self._sanitize_ops_text(g["title"])
            for sc in special_cases:
                if sc.get("why_special"):
                    sc["why_special"] = humanize_ops_text(
                        self._sanitize_ops_text(sc["why_special"])
                    )
            logger.info("Campaign synthesis 成功 [%s], %d groups + %d special_cases",
                         asin, len(groups), len(special_cases))
            return {"groups": groups, "special_cases": special_cases}
        except Exception as e:
            logger.warning("Campaign synthesis 失败 [%s]: %s", asin, e)
            return {"groups": [], "special_cases": [], "error": str(e)}


# 全局单例
reasoner = LLMReasoner()

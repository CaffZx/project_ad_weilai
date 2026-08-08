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
from app.models.campaign import CampaignUnit, SearchTermPromotionCandidate
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
- 根据核心词自然位稳定在约第3名、近7日排名持平，判断核心词自然位已较稳固。
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


def render_search_term_prompt_lines(search_term_data: dict | list) -> list[str]:
    """把搜索词 bundle 渲染为带窗口与样本标记的提示词行。"""
    if isinstance(search_term_data, list):
        summary: dict = {}
        terms = search_term_data
    else:
        summary = search_term_data.get("summary") or {}
        terms = search_term_data.get("terms") or []
    status = summary.get("search_term_fetch_status")
    if status:
        return [f"搜索词取数状态={status}（该活动不提供逐词搜索词证据）"]

    def _fmt(value: object) -> object:
        return "N/A" if value is None else value

    def _line(keyword: str, metrics: dict, window: str, insufficient: bool) -> str:
        return (
            f"      [{keyword}] [window={window}] "
            f"sample_insufficient_7d={'true' if insufficient else 'false'}: "
            f"cost=${_fmt(metrics.get('cost'))}, sales=${_fmt(metrics.get('sales'))}, "
            f"orders={_fmt(metrics.get('orders'))}, clicks={_fmt(metrics.get('clicks'))}, "
            f"impressions={_fmt(metrics.get('impressions'))}, "
            f"ACOS(raw)={_fmt(metrics.get('acos_raw', metrics.get('acos')))}%, "
            f"ACOS(corrected)={_fmt(metrics.get('acos_corrected'))}%, "
            f"CVR(raw)={_fmt(metrics.get('cvr_raw', metrics.get('cvr')))}%, "
            f"CVR(corrected)={_fmt(metrics.get('cvr_corrected'))}%, "
            f"data_min_age_days={_fmt(metrics.get('data_min_age_days'))}, "
            f"data_maturity={_fmt(metrics.get('data_maturity'))}, "
            f"maturity_basis={_fmt(metrics.get('maturity_basis'))}"
        )

    lines: list[str] = []
    for term in terms:
        if not isinstance(term, dict):
            continue
        keyword = str(term.get("keyword") or "?")
        insufficient = bool(term.get("term_sample_insufficient", False))
        metrics_7d = term.get("metrics_7d") or term
        lines.append(_line(keyword, metrics_7d, "7d", insufficient))
        metrics_14d = term.get("metrics_14d")
        if insufficient and isinstance(metrics_14d, dict) and metrics_14d.get("available"):
            lines.append(_line(keyword, metrics_14d, "14d", insufficient))
    return lines


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
- 指出矛盾点，当推荐的广告方向产生冲突时，写在overall_analysis里（如推进自然位 vs 优化ACOS 的权衡："推自然位需提价 20%，预期 ACOS 短期升至 35%+，这与当前 28% 的优化目标直接冲突。建议先守住 ACOS ≤30% 底线，仅对自然排名第 4-5 名的核心词小幅提价 10%，待自然位稳定后再降回。"）

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
- 判断是否推进自然位只能依据词级自然排名及其趋势；禁止使用自然单占比等聚合相对值作为推自然位价值的判据
""" + OPS_WRITING_RULES + REASONING_DECISION_RULES + """

## 输出格式

请严格按照以下JSON格式输出，不要包含markdown代码块：

{
  "recommended_directions": ["balance_maintain", "optimize_acos", "expand_keywords"],
  "reasoning": "【决策依据】\\n根据近7日ACOS由22%升至27%、CVR由32%降至23%的趋势，判断效率走弱但仍处于收割期可接受区间。\\n根据3个在投词ACOS超40%且花费集中，判断应优先否词或降价以控ACOS。\\n根据76个高转化词未收录、在投词整体ACOS约28%，判断可小批量试扩词但须设30%上限。\\n\\n【建议】\\n• 以平衡维持为主，维持现有结构守住排名与利润。\\n• 优化ACOS：处理超标词，整体控制在30%以内。\\n• 小批量试词5～10个，7日观察再放量。\\n\\n【后续关注】\\n若近7日ACOS持续高于35%或扩词批次7日ACOS超40%，需收紧或暂停对应动作。",
  "priority_order": ["balance_maintain", "optimize_acos", "expand_keywords", "push_natural"],
  "conflict_notes": "推自然位需提价 20%，预期 ACOS 短期升至 35%+，这与当前 28% 的优化目标直接冲突。建议先守住 ACOS ≤30% 底线，仅对自然排名第 4-5 名的核心词小幅提价 10%，待自然位稳定后再降回"
}

reasoning 必须含【决策依据】【建议】【后续关注】三段；【决策依据】每条独立一行且为「根据…，判断…」句式。"""


def _build_execution_system_prompt() -> str:
    return _EXECUTION_TASK_PROMPT.replace("{kb_content}", kb.build("execution_direction"))


_P3_TASK_PROMPT = """你是一个资深的亚马逊广告运营专家。基于知识库规则和诊断数据，同时给出目标ACOS和每日预算推荐。

重要：所有输出内容必须使用中文，禁止出现英文单词。数值字段使用英文key是内部格式需要。

## 概念澄清（重要，避免混淆）
- **目标ACOS**：运营设定的单一基准值，用于衡量实测 ACOS 偏离目标的程度，是本任务要输出的值。
- **实测ACOS / 当前ACOS**：广告实际跑出的 ACOS，是输入、不是目标。
- 知识库里的「ACOS上限 / 有效容忍上限 / ACOS容忍度加成」是对**实测ACOS**的天花板与触发判据，**不是**你要输出的目标ACOS——禁止把某个「ACOS上限」直接当作目标ACOS输出。
- 目标ACOS 必须落在用户消息给出的「目标ACOS取值区间 [最低, 最高]」之内（上限来自知识库，不得超出）。

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
    "reason": "【决策依据】\\n- 当前日均花费$65.8，日预算$100，花费率65.8%，预算未吃紧。\\n- 花费最高的词：triangle bikini($36.2/天, ACOS 48%)，string bikini($20.0/天, ACOS 41%)。\\n- 趋势：近7天花费从$87逐步降至$65，不是因为预算不足而是因为部分词ACOS过高被系统自然压低。\\n- 当前推进期+旺季准备，适度加预算抢流量是合理的。\\n\\n【建议】\\n建议日预算调整为$80(+21.5%)。增量集中分配给black bikini set(ACOS 28%, CVR 12.5%)和black string bikini(ACOS 18%, CVR 15%)等高效率词，不分配给triangle bikini等高ACOS词。\\n\\n【后续关注】\\n- 加预算后监控整体ACOS是否上升，若超过40%则停止增量\\n- 监控black bikini set的ACOS和CVR，若效率下降则重新分配预算"
  },
  "overall_reasoning": "【综合判断】\\nACOS目标和预算建议需联动：收紧ACOS降低低效花费，加预算把释放出的花费转移到高效率词上，在效率不崩的前提下抢旺季排名。\\n\\n【执行节奏】\\n建议先小幅收紧目标ACOS、观察3天整体ACOS变化，再决定是否加预算。加预算和收紧ACOS不建议同一天操作，避免数据波动难以归因。\\n\\n【风险提示】\\n- 精准ACOS 42%偏高，若精准位持续低效建议减少TOS投放比例\\n- 旺季CPC可能上涨，需预留预算弹性空间",
  "risk_warnings": ["精准ACOS 42%偏高，需重点优化精准投放", "旺季CPC可能上涨，预留预算弹性"]
}

（以上所有文本均为格式示例，请根据实际输入数据计算填充真实值。reasoning / reason / overall_reasoning 中不要照搬示例格式内的具体数值。）

## 字段语义说明
- budget_bid.current: **日均实际花费**（≈总花费÷天数）。从诊断数据中的"日均花费"字段取值。
- budget_bid.suggested: 建议的日均花费目标值。基于"日均花费"的当前水平 + 趋势 + 阶段策略给出。
- target_acos.recommended_target: 建议的目标ACOS百分比（**5% 取整**，如 25%/30%/35%），必须落在用户消息「目标ACOS取值区间」内
- 关键词数据中 spend 字段: 该词在 {days} 天窗口内的总计花费，除以天数才是日均花费。

## 硬性数值约束（P3 专用）
- 目标ACOS：必须落在用户消息「目标ACOS取值区间 [最低, 最高]」之内，5% 取整；上限来自知识库，不得超出。
- 每日预算的调整幅度遵循上述业务知识（知识库）中的数值规则，不另设硬编码上限。
- 如果数据不足以支撑判断，confidence设为"low"并在reasoning中说明
- ⚠️ reasoning/reason/overall_reasoning 中引用的关键词，必须来自"关键词级数据"列表中的实际关键词，禁止编造不存在于列表中的词名（P3 仅产出目标ACOS与每日预算，不输出关键词级 Bid 调整）

## reasoning / reason / overall_reasoning 文案要求（面向运营人员）
- 禁止提及任何内部约束规则词汇（如"硬约束""相对变化""合规""违规"等），用自然语言表达
- TACOS < 毛利率，才代表广告在盈利。引用TACOS必须带上毛利率，而非单纯对比ACOS和毛利率
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
  3. **【风险提示】**：2-3条在阈值边缘的关注提醒
- 不设字数限制，决策依据部分必须引用具体数值，禁止泛泛而谈。
"""


def _build_p3_system_prompt() -> str:
    return _P3_TASK_PROMPT.replace("{kb_content}", kb.build("p3_recommend"))


# ── Campaign 系统提示词 ────────────────────────────────────────────────────

_CAMPAIGN_SHARED_INTRO = """你是一个资深的亚马逊广告运营专家。基于知识库规则和用户消息中的活动数据，逐活动分析并给出调整/淘汰建议。

重要：所有输出内容必须使用中文（理由、证据、决策路径），JSON key 使用英文。

## 业务知识（必须严格遵循）
{kb_content}

## 业务知识优先级（必须遵循）
1. 本次注入 KB 中的禁止条件、保护条件和前置诊断步骤优先于任何经验判断。
2. 只使用本次注入且与当前广告方向匹配的动作规则。
3. 输入未提供的数据只能标注为缺失，不得当作 0、正常、异常或已完成的历史动作。
4. KB 已规定的淘汰、调整和数值约束必须按 KB 执行；不得以 Prompt 示例或单一字段自行创造更高优先级规则。

## 策略上下文解读
用户消息中的「策略上下文」包含该 ASIN 的产品阶段、广告目的、目标 ACOS、利润率、评分、退货率、库存天数、自然单占比等信息。这些是活动分析的"背景"，不需要在每个活动中重复输出。

## 活动列表
用户消息中的「活动列表」包含每个活动的：活动名、子ASIN、关键词、匹配类型、当前 Bid、当前 Budget、上线天数(注：-1 表示未知，勿当作新活动)、7日性能指标。

## 文本与数值自洽（强制，输出前自检）
reason 里的动作描述（含(3)调整建议）必须与你同时输出的 proposed 数值**严格同向同值**：
- 方向词「提升/调高/加大」⟺ proposed > current；「降低/下调/收紧」⟺ proposed < current；「维持」⟺ proposed = current。bid 与 budget 各自独立判断。
- reason 中提到的目标数值（如「提升至 $0.20」）必须等于对应的 proposed_bid / proposed_budget，不得是另一个数。
- 严禁出现「文本说提升、proposed 却更低」「文本说降到 $0.30、proposed_bid 却是 $0.20」这类自相矛盾。输出前逐条核对 reason 与 proposed_bid / proposed_budget 是否一致。"""

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
6. **细化执行**: KB 22 §2（精准调整规则）、KB 19 §5/§9（广告位决策/好坏判断）、KB 21 §0-4（淘汰规则）。
通用基准: KB 18 §1/§3、KB 19 §1-4/§6/§9、KB 22 §0。

> **自然排名信号**（活动列表含「自然排名」行时才有，仅精准）：排名上升且 ACOS 在容忍度内 → 倾向保护/推进该词（KB 22 §2.2 Ranking 保护、KB 19 §5 广告位）；排名下滑或「已掉榜」→ 命中 RANK_* 问题类型，谨慎降 bid/淘汰（如指向淘汰先走 KB 17 §7 淘汰前诊断）；无「自然排名」行 → 按现有指标逻辑，勿臆测排名。

## 输出格式
{
  "campaign_adjustments": [
    {
      "cid": "C3（原样回填输入中该活动的句柄，用于代码定位活动）",
      "action": "eliminate_to_low_bid_pool",
      "direction": {"bid": "down", "budget": "down"},
      "triggered_rule": "NO_CVR_HIGH_SPEND",
      "reason": "".join(["(1) 现状诊断", "(2) 原因分析", "(3) 调整建议"]),
      "proposed_budget": 1.0, "proposed_bid": 0.20,
      "evidence": ["7天花费$18.5", "7天订单0，CVR=0%"],
      "placement_adjustments": [
        {"placement": "头部", "action": "小涨", "evidence": "ACOS 25% 低于目标 30%，有花费有出单"}
      ],
      "review_level": "MANUAL_REVIEW"
    }
  ],
  "batch_summary": {"total_analyzed": 6, "to_eliminate": 1, "to_adjust": 3, "to_keep": 2, "overall_notes": "..."}
}

## 输出约束
### 必填结构字段
- **每个活动都必须填写**: cid（原样回填输入句柄）, action, direction, triggered_rule, proposed_budget, proposed_bid, evidence, review_level
- proposed_budget / proposed_bid 必须填写具体数值，禁止留 null
- 必须输出 placement_adjustments（三个广告位全部列出）。每个只填 `placement`（头部/其他/商品）+ `action`（维持/小涨/大涨/小降/大降）+ `evidence`。**禁止输出 current_pct/proposed_pct 数字**——这些由后端按 KB 19 §3 从当前加价比例 + action 自动计算
- **真实出价复合评估**：某广告位的真实出价 = Bid×(1+该位加价比例)，并非基础 Bid。当你同时调整 `proposed_bid` 与某广告位 `action` 时，两者会叠加放大/抵消该位的真实出价。给广告位 action 前必须以「当前真实出价」为基准评估复合后的真实出价变动幅度，勿只看加价比例步长；若复合后真实出价变动过大（如 >30%）而证据不足，应下调 action 档位（大涨→小涨/维持）或收敛 proposed_bid，并在 evidence 说明

### 淘汰活动
- 是否淘汰、淘汰保护和淘汰前诊断路径均以本次注入的 KB 21 §0-4 为准。
- action=eliminate_to_low_bid_pool 时，按 KB 21 §4 输出固定执行值，并给出命中的 triggered_rule 和 evidence。

### reasoning 文案禁则
- 禁止泄露内部约束术语（Bid步长/决策矩阵/规则编号/confidence等级）
- reason 中勿出现 NO_CVR_HIGH_SPEND 等触发码标记
- reason 使用三部分结构：(1) 现状诊断 → (2) 原因分析 → (3) 调整建议"""
)

_CAMPAIGN_BROAD_PROMPT = (
    _CAMPAIGN_SHARED_INTRO
    + """

## 角色
你正在分析**广泛/词组广告活动**（BROAD / PHRASE / AUTO 匹配类型）。调整维度为 Budget → Bid → SearchTerm（否词/提词）。

## KB 引用指引（决策链：诊断 → 取值 → 动作）
1. **先算容忍度**: KB 17 §2 + KB 15 §4（目标ACOS + 阶段加值 + ranking/旺季/promotion 加值）。所有 ACOS 高低判断必须对比该值，禁止硬编码"ACOS>40%"。
2. **再定问题类型**: KB 17 §1（BLOCKED / SAMPLE_INSUFFICIENT / CVR_WEAK / HIGH_ACOS_* / BUDGET_* / KEYWORD_POOL_DIRTY / ALL_HEALTHY）。广泛/词组活动尤其关注 KEYWORD_POOL_DIRTY、HIGH_ACOS_* 的否词治理路径。
3. **查动作矩阵**: KB 17 §3 按广告方向 × 问题类型取有序动作（广泛活动多走 §3.2 expand_keywords / §3.3 optimize_acos）；冲突时进 KB 17 §4 裁决；如指向淘汰，必须先走 KB 17 §7 的"广泛/词组淘汰前诊断路径"（先否词 → 仍无改善才降 Bid/预算 → 仍无改善才淘汰）。
4. **取约束数值**: KB 15 §1（Bid 公式/保护规则/硬上下限）、KB 15 §2（预算范围 + 淡旺季系数）。
5. **应用幅度系数**: 最终幅度 = KB 19 基础幅度 × KB 17 §5.2 阶段系数 × KB 17 §5.3 淡旺季系数。
6. **否词决策**: 仅对输入中带有逐词搜索词数据的活动判断否词；先读 KB 30 §2（必需数据）确认数据可用，再按 KB 30 §3 判断相关性，最后走 KB 30 §5 准入规则和 KB 30 §6 精准/词组选择。7d 是动作基线，14d 仅是长尾/低流量/样本不足词的辅助观察，不能单独触发动作。词级样本不足按 7d「点击<10 且花费<max($15, 目标 CPA)」判定；无逐词搜索词数据或取数状态为跳过/失败时，`negative_keywords=[]` 且不得输出该活动的提精准候选。证据中的点击、花费、订单、销售额、ACOS、CVR 数字必须明确写 7d 或 14d；校正指标不可用时不得用 raw 指标替代动作判断。
7. **淘汰决策**: KB 21 §0-4。

## 输出格式
{
  "campaign_adjustments": [
    {
      "cid": "C3（原样回填输入中该活动的句柄，用于代码定位活动）",
      # 示例为调整场景（加预算），避免 eliminate 示例的 proposed_budget/proposed_bid
      # 与淘汰规则（固定 $1 / min bid）矛盾——淘汰由代码展开，不受 LLM 输出数值控制
      "action": "adjust_budget",
      "direction": {"bid": "keep", "budget": "up"},
      "triggered_rule": "BUDGET_EXHAUSTED",
      "reason": "".join(["(1) 现状诊断", "(2) 原因分析", "(3) 调整建议"]),
      "proposed_budget": 12.0, "proposed_bid": 0.40,
      "evidence": ["预算利用率连续3天>90%", "近7天ACOS低于目标"],
      "negative_keywords": [
        {"keyword": "wedding dress", "match_type": "NEGATIVE_EXACT", "reason": "7天点击12次、零订单、无转化"}
      ],
      "review_level": "MANUAL_REVIEW"
    }
  ],
  "exact_promotion_candidates": [
    {
      "cid": "C3",
      "search_term": "sticky bra for dress",
      "keyword_class": "long_tail",
      "relevance_tier": "R1",
      "reason": "搜索词与产品本体直接相关",
      "evidence": ["来自该活动本批搜索词报告"]
    }
  ],
  "batch_summary": {"total_analyzed": 6, "to_eliminate": 1, "to_adjust": 3, "to_keep": 2, "overall_notes": "..."}
}

## 输出约束
### 必填结构字段
- **每个活动都必须填写**: cid（原样回填输入句柄）, action, direction, triggered_rule, proposed_budget, proposed_bid, evidence, review_level
- proposed_budget / proposed_bid 必须填写具体数值，禁止留 null

### action 合法枚举（必须且只能输出其中之一）
- `keep`：保持
- `adjust_bid`：调出价（配合 proposed_bid）
- `adjust_budget`：调预算（配合 proposed_budget）
- `eliminate_to_low_bid_pool`：淘汰至低价捡漏（复合动作，代码展开为迁组+预算$1+bid min）
- `paused_campaign`：**暂停活动（关停）**。出 LLM 即由代码翻译为后端 `paused`；仅活动级状态变更，proposed_budget/proposed_bid 数值会被代码忽略，勿用它承载"暂停+调价"组合（暂停优先）
- **禁止输出 `adjust_placement`**（广泛/词组/自动禁止广告位调整，ONT-002；placement_adjustments 一律 N/A）
- 多维同时调整时 action 只能选一个主动作，其余靠 direction/proposed 数值表达

### triggered_rule 命名空间（必须遵守）
- 只输出 **KB17 问题类型码**：BLOCKED_INVENTORY / STAGE_EXPIRED_TESTING / SAMPLE_INSUFFICIENT / CVR_WEAK / HIGH_ACOS_NO_ORDER / HIGH_ACOS_LOW_ORDER / HIGH_ACOS_WITH_ORDER / BUDGET_NO_SPEND / BUDGET_CANT_SPEND / BUDGET_EXHAUSTED / RANK_OPPORTUNITY / RANK_DROPPING / KEYWORD_POOL_DIRTY / KEYWORD_POOL_EXHAUSTED / PLACEMENT_INEFFICIENT / PORTFOLIO_BOTTLENECK / ALL_HEALTHY
- 禁止填入淘汰条件码（如 NO_CVR_HIGH_SPEND / IRRELEVANT_NO_IMPROVEMENT 等 KB21 condition_code）——那是另一命名空间

### review_level 枚举
- `AUTO_APPROVED` / `MANUAL_REVIEW` / `HIGH_RISK_REVIEW`（不可输出其他值）
- 仅对当前活动输入中实际带有逐词搜索词数据时判断 `negative_keywords`；无逐词搜索词数据、取数跳过或失败时必须输出 `[]`，禁止 null。
- 每个否词必须来自该 cid 实际展示的搜索词，包含 keyword、match_type（仅允许 NEGATIVE_EXACT）、reason。reason/evidence 中凡引用数字，必须同时标明 7d 或 14d；14d 只能作为辅助观察，不能独立触发否词。

### 预算调整资格（必须遵守，代码已判定）
- 加预算仅允许在活动级「加预算资格(代码判定)=✅ 表现好才可加预算」且策略上下文「库存≥30天」时；幅度按活动级「预算资格档位(代码判定)」：`>90%`→可大涨、`70-90%`→可小涨、`50-70%`→维持、`<50%`→不加预算。
- reason 中凡建议加预算，必须引用代码判定的资格字段与档位，不得自行推导门槛（如"表现好就加"）；`budget_increase_eligible=false` 或库存不足时不得加预算。

### 正向搜索词候选（新增精准活动接线）
- `exact_promotion_candidates` 必须输出数组；无候选时填 `[]`，禁止 null。
- 每条必须带当前批次原样 `cid`、该 cid 搜索词报告中原样出现的 `search_term`、`keyword_class`、`relevance_tier`、reason、evidence。
- **标注 ≠ 准入**：你只做语义识别（判相关性 + R1）；订单/ACOS 门槛（订单≥3 且 ACOS≤目标 / 词根聚合）由代码用真实搜索词数据校验。低置信或语义不确定的候选不标。
- 词级样本不足词（sample_insufficient_7d=true）可保留审阅（14 天仅辅助观察，不能独立触发动作）；活动级样本不足（SKIPPED_CAMPAIGN_SAMPLE_INSUFFICIENT）的活动禁止输出候选。
- 只输出语义相关且至少 R1 的正向候选；禁止编造输入外搜索词、禁止输出订单/花费/销售额/ACOS/CVR 等数值字段。
- 这是候选标注，不是最终建活动决策；Python 会按真实搜索词报告重新校验订单、ACOS、已有精准词和词根聚合。

### 淘汰活动
- 是否淘汰、淘汰保护和淘汰前诊断路径均以本次注入的 KB 21 §0-4 为准。
- action=eliminate_to_low_bid_pool 时，按 KB 21 §4 输出固定执行值，并给出命中的 triggered_rule 和 evidence。

### reasoning 文案禁则
- 禁止泄露内部约束术语（Bid步长/决策矩阵/规则编号/confidence等级）
- reason 中勿出现 IRRELEVANT_NO_IMPROVEMENT 等触发码标记
- reason 使用三部分结构：(1) 现状诊断 → (2) 原因分析 → (3) 调整建议"""
)


def _build_campaign_system_prompt(
    task_type: str = "exact",
    ad_directions: list[str] | tuple[str, ...] | str | None = None,
) -> str:
    # 精准/广泛各取切片化 preset（2026-06-24）：精准带广告位节、广泛带否词节，互不注入对方噪声
    template = _CAMPAIGN_EXACT_PROMPT if task_type == "exact" else _CAMPAIGN_BROAD_PROMPT
    return template.replace(
        "{kb_content}",
        kb.build_campaign_adjustment(task_type, ad_directions),
    )


# ── Campaign 新增活动 Prompt (KB 16 + 06) ────────────────────────────────────

_NEW_CAMPAIGN_PROMPT = """你是亚马逊广告新增活动决策助手。基于知识库 KB 16《新增活动规则》、KB 06《关键词类型规则》和 KB 28 §2《相关性分级规则》，为候选关键词判断**是否值得新建活动**以及**关键词类别**。

重要：输出中文，JSON key 用英文。

## 业务知识（判断依据）
{kb_content}

## 任务范围（重要）
你只做三类判断 + 文本输出：
1. **action**：该词是否值得新建活动（create / skip）。参考 KB 16 §1 触发场景与 §6 阻断精神，以及【今日总纲】。
2. **keyword_class**：按 KB 06 判该词类别（generic / long_tail / competitor / brand / custom）。
3. **relevance_tier**：按 KB28 §2 判该词与本产品的相关性档位（R1 / R2 / R3 / R4）。
4. **文本**：reason / evidence / negative_strategy。

**禁止**输出 bid / budget / campaign_name / match_type / primary_placement —— 这些由代码按 KB 16 §2/§3/§4/§5 确定。

## 输入
- 策略上下文（ASIN 级，含【今日总纲】posture_brief — 必须遵循；含运营配置的「目标关键词类型」）
- **本产品标题** + **已投放关键词**（运营/系统已认定与本产品相关的词，作相关性参照）
- 候选词列表：每个含 keyword_text / search_volume(流量词库搜索量) / search_rank(流量词库搜索排名) / week_search_volume(周搜索量) / week_rank(词的周排名) / natural_rank(当前自然位) / rank_trend(近7天自然位序列) / rank_tier(自然位分位) / sponsored_rank(广告排位) / history_state / trigger_scene / source。来源主要为「流量词库」与「自然位机会词」。

## 来源与最终匹配契约
- `source=flow` 的候选若 action=create，代码会固定组装为 BROAD 探索活动；因此 `negative_strategy` 必须填写非空的搜索词观察/否词策略。long_tail 只是关键词类别，不能据此留空。
- `source=ranking_opportunity` / `source=competitor` 的最终匹配类型仍由代码按既有来源规则决定；你不输出、不猜测 match_type，`negative_strategy` 可以填空字符串。

## 排名数据使用规则（必须遵循）
- `search_rank` 是流量词库的搜索排名，反映词的热度/竞争位置；它**不是** `week_rank`，也不能单独证明该词与本产品相关。
- `natural_rank` 是当前自然位；有值时即可作为产品在该词下的排名事实。自然位数字越小越靠前，但不可脱离标题属性单独建词。
- `rank_trend`、`rank_tier`、`sponsored_rank` 是附加排名信号，仅在 `history_state=ok` 时使用；可用于判断趋势、排名层级和广告竞争，但不能替代产品属性相关性判断。
- `history_state` 只影响 `rank_trend`、`rank_tier`、`sponsored_rank` 是否可用：`not_eligible` 是未查询补充数据，`query_failed` 是查询失败，`empty` 是无补充记录。它们不表示没有当前自然位，也不得据此降低相关性；应以标题、已投词和其余可用信号判断。

## 相关性判断与档位（首要，KB28 §2 + KB 06 §3 相关性精神）
**综合权衡**可用信号 → 给出 relevance_tier，不要只看其中一个：
  ① 当前自然位 natural_rank（靠前=数字小=事实相关强），以及 `history_state=ok` 时的 rank_trend
  ② search_rank / week_rank / search_volume / week_search_volume 的热度与位置
  ③ rank_tier / sponsored_rank 的辅助竞争信号 ④ 与**标题具体属性**的相关度。
- **R1 精确相关**：词义=产品本体，或精确匹配标题核心属性（品类+核心属性词），或 natural_rank 有值（亚马逊确实让本产品排该词=事实相关）。
- **R2 扩展相关**：同类目近义、上位/下位词、强相关使用场景词；有搜索量/排名数据支撑。
- **R3 试探相关**：可能相关、需验证（跨类目联想词等）；**仅测试期可承接，且 reason 必须写明"为何判定可能相关"**。
- **R4 风险相关**：词义偏离、易招无效点击 → **一律 action=skip**。
- **属性级精准，不是品类级**：仅"同品类"不够。例：标题"短裙 mini skirt"→"中长裙 midi/maxi""连衣裙 dress"长度/款式不符 → 判 R4 并 skip；reason 点明属性是否吻合。
- **五点 + 运营搜索词是强锚点**（若上下文提供）：五点描述产品功能/材质/场景/人群，是相关性判断的**事实依据**——候选词若命中五点中的用词或同义表达 → 倾向 R1。五点可以补标题信息不足，但不参与搜索量权衡。
- **信号怎么综合**：可用自然位/周排名靠前 + 标题属性吻合 → 倾向 R1；**搜索量高但与标题/五点属性不符 → 不因量大就抬档**（量大≠相关）；**搜索量低但属性精确吻合或有可用自然位 → 仍可 R1**（低量精准长尾是运营偏好，勿因量小误杀）。历史状态非 ok 时，不得把缺失数据当作负面证据。
- **锚点稀薄保护**：当"已投放关键词"为空、标题信息少（新品/小 ASIN）时，不要因参照少就过度 skip——以标题为主判相关性。

## 词型偏好与运营目标类型（KB 06 + 运营配置）
- **优先高相关长尾词**（多词、含产品具体属性 → 相关性高、竞争低、ACOS 可控）；匹配类型由来源和代码决定，勿把 long_tail 本身当作 EXACT 结论。
- **审慎对待大词/泛词**（单词或品类大词，如 "skirt"/"dress"）：相关性弱、ACOS 难控；除非测试期/引流型否则倾向 skip；收割/盈利/维持期尤不应新建大词。
- **运营目标关键词类型（软偏好）**：上文「目标关键词类型」是运营配置的偏好。在相关性达标前提下，**优先选择属于该类型的词**；不属于该类型的词需要更强相关性（R1）才建。这是倾向性引导，不是硬性排除。

## 输出 JSON（严格 schema，禁 markdown 围栏）
{
  "new_campaigns": [
    {
      "keyword_text": "fishnet stockings women plus size",
      "action": "create",
      "keyword_class": "long_tail",
      "relevance_tier": "R1",
      "negative_strategy": "运行7天后读取搜索词报告，按相关性和样本门槛审阅精准否词候选",
      "reason": "(1) 现状：流量词库有搜索量且当前尚未覆盖；(2) 原因：长尾词匹配标题属性 fishnet/plus size，相关性高；(3) 建议：纳入新活动验证。",
      "evidence": ["搜索量 156", "相关性 R1：匹配标题具体属性"]
    },
    {
      "keyword_text": "black fishnet stockings plus size",
      "action": "create",
      "keyword_class": "long_tail",
      "relevance_tier": "R1",
      "negative_strategy": "运行7天后读取搜索词报告，按相关性和样本门槛审阅精准否词候选",
      "reason": "(1) 现状：流量词库有搜索量且含颜色修饰词 black；(2) 原因：长尾词匹配标题属性 fishnet/plus size，且含产品可用颜色 black；(3) 建议：纳入新活动验证，指派 black 颜色子 ASIN。",
      "evidence": ["搜索量 89", "相关性 R1：匹配标题具体属性 + 颜色 black"],
      "color_flags": {"black": true}
    },
    {
      "keyword_text": "halloween fishnet stockings",
      "action": "skip",
      "keyword_class": "long_tail",
      "relevance_tier": "R2",
      "reason": "(1) 现状：流量词库有搜索量，含节日词 halloween；(2) 原因：距 Halloween 约 85 天 > 60 天，非投放期；(3) 建议：Halloween 前 60 天再评估。",
      "evidence": ["搜索量 200", "距 Halloween 85 天"],
      "holiday_flags": {"halloween": true}
    }
  ]
}

## 判断与文案规则
- **每个词必须输出 relevance_tier（R1/R2/R3/R4）**；**R4 一律 action=skip**；R3 仅测试期可 create，且 reason 必须写明"为何判定可能相关"（KB28 §2）。
- 不值得建的词（**与产品无关** / 相关性差 / 搜索量虚高但无意图 / 与现有词重复语义）→ action=skip，reason 说明原因。
- **每个 create 的词，reason 第(2)段必须写出与本产品的相关性依据**（如何与**标题具体属性**/已投词关联）；说不出相关性的不得 create。
- `source=flow` 且 action=create 时，negative_strategy 必须填写非空；其他来源按上面的来源契约处理。不得根据 keyword_class 猜测最终匹配类型。
- reason 三段式：(1) 现状诊断 (2) 原因分析 (3) 建议；禁用规则编号 / 内部术语；evidence 引用具体数值。

## 颜色词与节日词标记（可选字段，仅在命中时输出）
### color_flags
- 仅当候选词中包含产品颜色的具体表述时输出：{颜色英文: true}。示例：「fishnet stockings black」→ `{"black": true}`；「red lace top」→ `{"red": true}`。
- 颜色名必须在上下文「产品可用颜色」列表中。一个词可含多个颜色（如「red and black fishnet」→ `{"red": true, "black": true}`）。
- 非颜色词（不含颜色修饰的通用词）→ 不输出 color_flags 字段或输出 `{}`。
- **禁止编造颜色**：只能使用上下文给出的「产品可用颜色」列表中的值。

### holiday_flags
- 仅当候选词带节日意图时输出：{节日英文小写: true}。示例：「halloween fishnet」→ `{"halloween": true}`；「christmas gift」→ `{"christmas": true}`。
- **节日词谨慎创建**：以「当前日期」为基准，距节日 **>60 天** → 一律 action=skip，reason 写明「距 X 节日约 Y 天，非投放期」；≤60 天可创建，reason 写明距节日天数作为投放窗口参考。
- 已投词中的节日词不构成扩词依据——那可能是在当时节日季投放的，不代表当前应该扩。
- 不明显的节日词（如「gift」「party」无特定节日指向）→ 不输出 holiday_flags。
- 非节日词 → 不输出 holiday_flags 字段或输出 `{}`。
"""


def _build_new_campaign_prompt() -> str:
    return _NEW_CAMPAIGN_PROMPT.replace("{kb_content}", kb.build("new_campaign"))


# ── Campaign 汇总合成 Prompt ─────────────────────────────────────────────────

_CAMPAIGN_SYNTHESIS_PROMPT = """你是亚马逊广告运营专家。把若干单活动调整建议合成为运营可读的「分组叙事 + 特殊调整尾部清单」。

本任务**不做广告决策**——每条建议的动作(action)和理由(reason)已在上游确定。你只需读懂已有结论，按语义把"同动作 + 同理由"的建议聚类，并为每组写运营叙事。无需任何额外的广告领域规则。

## 任务
按"共同原因"把建议聚成 **5-7 组**——组数按**实际共同原因的数量**决定：原因多就接近 7 组、原因少就 5 组左右，**不要为凑满 7 组而硬拆，也不要把不同原因硬并**。每组给出标题 + 一段 2-4 句运营叙事；
**严禁产出多个理由实质相同的组**：归因 / `common_reason_code` 相同或高度重叠的必须合并为同一组——每组"共同原因"互相区分、互不冗余。
**绝大多数活动必须落入某个分组**；`special_cases`（难归组的特殊活动）**数量控制在 5-15 条**——只放真正难归组的极少数，宁可多归一组，也不要把活动大量堆进 special。

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

_CAMPAIGN_OVERVIEW_PROMPT = """你是资深亚马逊广告策略分析师。基于知识库规则，把这个 ASIN 的【策略上下文】与【业务规则】做匹配，产出"今天广告活动调整的宏观总纲"。这是逐活动分析之前的定调，不点评任何单个活动。

重要：输出中文，JSON key 用英文。

## 业务知识（判断依据，必须据此推理）
{kb_content}

## 你的任务：匹配 → 判断 → 定调
1. 把给定的策略上下文（产品定位/阶段/淡旺季/广告目的/目标ACOS/数据信号）逐项对照上面的业务规则，找出当前最关键的矛盾或机会（如：阶段诉求与淡旺季诉求的张力、产品定位的优先级保护、广告目的的方向倾向）。
2. 由判断推导出整体调整基调，再落到具体的广告方向主辅安排。

## 硬性约束
- **重判断、轻数字**：数字只作判断依据，**禁止在输出里复述**"总预算$X / N个活动 / X个达标"这类现状数字（前端另有展示）。要说的是"为什么这么调"，不是"现在是什么数字"。
- **禁止**出现"已淘汰N个/已提价/预计下调$X"这类分析结果——那是后续汇总的事。
- 宏观视角，不逐个活动点评。
- 广告方向统一用中文：推进自然位 / 新增扩词 / 优化ACOS / 平衡维持。
- 数据缺失（N/A）的维度：明确不基于它判断、不臆测，既不当作"正常/安全"、也不当作"风险/不足"；不得因数据缺失就反向施加阻断（如缺库存数据≠库存不足，不得据此禁止对自身指标健康的活动加预算/调价等动作）。只有规则中有明确阈值且数值确认越线（如库存确认<7天）才触发对应阻断。

## 输出格式（纯 JSON，不含 markdown 代码块标记）
{
  "assessment": "核心判断：用业务规则匹配现状，指出当前 ASIN 的关键矛盾/机会与整体定调，并说明命中了哪条业务逻辑（如：收割利润期要求收紧效率，但旺季准备要求保排名保转化，二者存在张力；产品定位 P0 受保护规则约束、核心权重不可降——故基调定为'控效率但不牺牲排名'）。2-4 句。",
  "direction": "宏观方向：基于判断给整体打法——以哪个广告方向为主、哪个为辅，低效流量如何处理，核心词/排名如何保护，并给原因。2-4 句，不含活动数字。",
  "posture_brief": "给后续逐活动分析的统一判断基准，指令式、可直接套用：① 主/辅方向；② 必须保护、不可下调的对象；③ 优先处理的对象；④ 红线。例：主优化ACOS、辅平衡维持；P0核心词与排名型Bid不下调；零花费与高ACOS无单活动优先收缩；广泛词先否词再降价；缺数据维度不主动加价。",
  "allow_growth_analysis": true
}

## allow_growth_analysis 判定准则（增长门禁，控制下游"新增扩词"与"淘汰复评"两道增长流）
- 仅当**明确判断今日不应当做增长分析**时输出 `false`（必须为 JSON 布尔值 true/false，不可输出字符串）：
  库存可售天数触红线（<7天）、退货率/评分触红线、经营模式已是清货优先、ACOS 危机未解、
  预算吃紧需收缩而非扩张、本 ASIN 处于"立即退出"或"控量清货"运行态等收紧场景。
- 当**应当增长 / 信号不明确 / 拿不准**时输出 `true` 或不出现该键：旺季准备、词池机会、
  淘汰补位、量价齐升、扩词有助于补位/引流等扩张场景，以及任何不确定情况。
- 准则由你自洽：复用上方已注入的经营模式/库存/退货率/评分/旺季/目标ACOS 等上下文，
  不要求穷举条件。取倾向是 fail-open——宁可放过候选词，不要因 LLM 误判关掉本该跑的增长流。
"""


def _build_campaign_overview_prompt() -> str:
    return _CAMPAIGN_OVERVIEW_PROMPT.replace("{kb_content}", kb.build("campaign_overview"))


# ── 核心词语义判定 Prompt（KB29 §1-6）────────────────────────────────────────

_SEMANTIC_CORE_PROMPT = """你是亚马逊产品广告专家。给定一个产品的 Listing 信息和若干投放关键词，判断每个词的语义相关性、是否存在语义冲突、是否为语义核心词。

输出中文，JSON key 用英文。

## 业务知识
{kb_content}

## 判断规则

### 语义冲突（semantic_conflict）：4 种任一命中即 fail
1. 品类不一致 — 关键词品类 ≠ 产品实际细分品类
2. 属性不符 — 关键词的款式/结构/功能/人群/场景与 Listing 不符
3. 变体不属于当前子 ASIN — 关键词的颜色/尺码等不可售
4. 非产品意图词 — 纯价格/促销/平台活动词，不表达产品本体

### 语义核心（semantic_core）：冲突 pass 的前提下，满足 R1 精确相关
- R1：关键词直接指向产品细分品类 / 核心属性 / 真实变体 / 核心人群与场景

## 输出格式（纯 JSON，不含 markdown 代码块标记）
{
  "keywords": [
    {
      "keyword_text": "str",
      "semantic_conflict": "pass" | "fail",
      "conflict_reason": "fail 时写原因；pass 时为空",
      "semantic_core": true | false,
      "semantic_evidence": ["语义核心的证据描述"]
    }
  ]
}
"""


def _build_semantic_core_prompt() -> str:
    return _SEMANTIC_CORE_PROMPT.replace("{kb_content}", kb.build("semantic_core"))


# ── Campaign 预算回算 Agent Prompt（KB23）────────────────────────────────────

_BUDGET_REALLOC_PROMPT = """你是亚马逊广告预算回算专家。依据下方知识库（KB23 广告组合与预算分配规则），把父目标预算合理分配到 3 个活跃组合（精准主力组 / 精准测试组 / 自动广泛组）。

重要：输出中文，JSON key 用英文。这里分配的是**组合层预算约束值**（控制层 cap），不是组内活动预算之和。

## 业务知识（判断依据，必须据此推理）
{kb_content}

## 输入说明（数值已由代码算好，禁止重算）
- `parent`：
  - `parent_target_daily_budget` = 父目标日预算
  - `budget_pool` = 父目标 + 允许净增（**绝对硬顶**，3 组 proposed 之和不得超此值）
  - `available_for_increase` = 淘汰释放 + 允许净增（**参考增量额度，非硬约束**）
  - `low_bid_retention_release` = 本轮淘汰活动在活动层释放的预算
  - `priority_context` = 目标ACOS/有效容忍上限/产品定位/淡旺季/广告目的/广告方向（判断倾斜的依据）
- `groups[]`（每个活跃组合一条）：
  - `current_group_budget` = 该组合在 Amazon 的**真实当前预算**（来自 portfolio MCP；若为 0 则该组之前不存在，可从 0 起建）
  - `daily_spend` = 该组合**日均花费**（已按 7 天平均换算，无需再除）。**None 表示 MCP 未返回花费数据**，此时不计算组合利用率
  - `spend_utilization` = 组合利用率 = daily_spend / current_group_budget（**判断"花完没"的核心信号**；current_group_budget=0 时为 null）
  - `acos_7d` = 该组合近 7 天 ACOS（表现好坏；None 表示无数据）
  - `group_requested_delta` = 组内活动**想加/减多少**（净需求信号，不是绝对预算）
  - `new_requested_delta` = 其中来自本轮**新建活动**的需求（current=0 全是净增）；不得为新活动稀释推词预算
  - `campaign_budget_sum_after` = 本轮挪组+新增后，该组合内活动预算之和（统计值，可对比 `current_group_budget` 判断瓶颈：活动之和接近或超过组合预算 → 组合预算可能是瓶颈，加组合预算才有效）
  - `group_budget_floor` = 挪组后该组合内**活动预算的最大值**（组合预算不得低于此值，否则最高预算活动无法运行）；无活动时为 0
- `low_bid_group`：低价捡漏组，固定 $1、不参与分配（代码已处理，不要出现在输出中）。

## 你的任务
1. 起点 = 各组 `current_group_budget`（真实 portfolio 预算）。
2. 各组 `proposed_group_budget` = current + 你的调整量。
3. **优先给"花完且表现好"的组合加预算**：利用率高（`spend_utilization` 接近或超过 1）且 ACOS 达标（`acos_7d` 在目标内）的组合该加就加。
4. 按 KB §7 决定倾斜方向（数据健康→稳定；P0/P1/ranking/旺季→向主力组倾斜）。
5. 每组在 `reason` 里用运营可读中文说明为什么这么分。

## 硬性约束（违反将被拒绝回落规则引擎）
- 各组 `proposed_group_budget` **不得低于** `group_budget_floor`（组合预算不能低于组内最大活动预算，否则该活动无法运行。无活动时 floor=0，不限制）——**优先级最高**
- 3 组 `proposed_group_budget` 之和 ≤ `budget_pool`（父目标硬顶）
- 各组 `proposed_group_budget` ≥ 0
- **低价捡漏组不得出现在 budget_groups 里**（KB GROUP-004）

## 输出格式（纯 JSON，不含 markdown 代码块标记）
{
  "allocation_method": "base | proportional | weighted_main",
  "parent": {
    "proposed_total_group_budget": <3组之和>,
    "explanation": "为什么这么分：倾斜了哪个组、依据什么（需求/优先级/自然流量）。"
  },
  "budget_groups": [
    {"group": "精准主力组", "current_group_budget": <num>, "proposed_group_budget": <num>, "reason": "..."},
    {"group": "精准测试组", "current_group_budget": <num>, "proposed_group_budget": <num>, "reason": "..."},
    {"group": "自动广泛组", "current_group_budget": <num>, "proposed_group_budget": <num>, "reason": "..."}
  ]
}
"""


def _build_budget_realloc_prompt() -> str:
    return _BUDGET_REALLOC_PROMPT.replace("{kb_content}", kb.build("budget_reallocation"))


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
            # [产品阶段] 已由经营模式替代，不再注入 LLM prompt
            # parts.append(f"  - 产品阶段: {strategy.get('product_stage', '?')}")
            parts.append(f"  - 淡旺季: {strategy.get('season_stage', '?')}")
            parts.append(f"  - 经营模式: {strategy.get('operating_mode')}")
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
            # [产品阶段] 已由经营模式替代，不再注入 LLM prompt
            # f"  - 产品阶段: {strategy.get('product_stage', '?')}",
            f"  - 淡旺季: {strategy.get('season_stage', '?')}",
            f"  - 经营模式: {strategy.get('operating_mode')}",
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
                label="execution",
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
            # [产品阶段] 已由经营模式替代，不再注入 LLM prompt
            # f"  - 产品阶段: {strategy.get('product_stage', '?')}",
            f"  - 淡旺季: {strategy.get('season_stage', '?')}",
            f"  - 经营模式: {strategy.get('operating_mode')}",
            "",
            "## 策略层",
            f"  - 广告目的: {tactics.get('ad_purposes', [])}",
            f"  - 关键词类型: {tactics.get('target_keyword_strategy', [])}",
            "",
        ]

        # 目标ACOS 取值区间（修复4）：代码按 阶段×层级×目的 算出 [下限,上限]，注入供 LLM 在区间内出单值
        from app.core.recommender import compute_target_acos_band
        from app.config.settings import settings as _settings
        _acos_floor, _acos_ceiling = compute_target_acos_band(
            _settings.thresholds_config.get("target_acos", {}),
            strategy.get("product_stage"), strategy.get("product_level"),
            tactics.get("ad_purposes") or [],
        )
        parts += [
            "## 目标ACOS 取值区间（必须在此区间内给出一个值）",
            f"  - 最低目标ACOS: {_acos_floor}%（按广告目的，且不低于全局最低）",
            f"  - 最高目标ACOS: {_acos_ceiling}%（来自知识库 阶段×层级 ACOS上限）",
            f"  → 在 [{_acos_floor}%, {_acos_ceiling}%] 内输出一个目标ACOS（5% 取整）。区间是工作边界，输出是其中的单一值。",
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
                label="p3",
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
                label="analyze",
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
        # 预算为正即注入利用率（含 0 花费 → 显式 0.0%，让 LLM 区分"已知 0%"与"字段缺失/N/A"）。
        # budget<=0（未知）时不注入 → LLM 按缺失处理，不得当 0。
        if cu.current_budget > 0:
            summary["budget_utilization_pct"] = round(p.cost / (cu.current_budget * 7) * 100, 1)
        # P0-E 预算资格：利用率档位 + 加预算资格（代码预计算，LLM 只在此范围内选幅度）
        if "budget_utilization_pct" in summary:
            _pct = summary["budget_utilization_pct"]
            if _pct > 90:
                summary["budget_utilization_tier"] = ">90%（可大涨档）"
            elif _pct >= 70:
                summary["budget_utilization_tier"] = "70-90%（可小涨档）"
            elif _pct >= 50:
                summary["budget_utilization_tier"] = "50-70%（维持档）"
            else:
                summary["budget_utilization_tier"] = "<50%（花不完档，不加预算）"
            summary["budget_increase_eligible"] = _pct >= 70
        if keyword_class:
            summary["keyword_class"] = keyword_class
        if is_core:
            summary["is_core"] = True
        # 广告位加价比例 (KB 19 §5 决策矩阵依赖)
        summary["_placement_pcts"] = {
            "头部": cu.tos_bid_pct,
            "商品": cu.pp_bid_pct,
            "其他": cu.ros_bid_pct,
        }
        # 自然排名（周排名）：仅精准注入；广泛流排名概念模糊，不给
        if cu.match_type == "EXACT" and (cu.natural_rank is not None or cu.near_natural_rank is not None):
            summary["_natural_rank"] = {
                "cur": cu.natural_rank,
                "near": cu.near_natural_rank,
                "change": cu.rank_change,
            }

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
            # [产品阶段] 已由经营模式替代，不再注入 LLM prompt
            # f"  - 产品阶段: {strategy_context.get('product_stage', '?')}",
            f"  - 产品定位: {product_level_with_code(strategy_context.get('product_level', '')) or '?'}",
            f"  - 淡旺季: {strategy_context.get('season_stage', '?')}",
            f"  - 经营模式: {strategy_context.get('operating_mode')}",
            f"  - 广告目的: {strategy_context.get('ad_purposes', [])}",
            f"  - 目标关键词类型: {strategy_context.get('target_keyword_strategy', [])}",
        ]
        margin = strategy_context.get("margin")
        ctx_parts.append(f"  - 毛利率: {'%.1f%%' % (margin * 100) if margin is not None else 'N/A'}")
        ctx_parts.append(f"  - 评分: {strategy_context.get('rating', 'N/A')}")
        ctx_parts.append(f"  - 退货率: {'%.1f%%' % strategy_context['refund_rate'] if strategy_context.get('refund_rate') is not None else 'N/A'}")
        inv_days = strategy_context.get("inventory_days")
        ctx_parts.append(f"  - 库存可售天数: {'%.0f天' % inv_days if inv_days is not None else 'N/A'}")
        # P0-E 预算资格：代码已算好，透传 LLM（LLM 不必自行重算容忍上限/CPA/平均订单金额）
        _acos_tol = strategy_context.get("effective_acos_tolerance")
        if _acos_tol is not None:
            ctx_parts.append(f"  - 有效容忍上限(代码已算): {_acos_tol}%")
        _cpa = strategy_context.get("target_cpa")
        if _cpa is not None:
            ctx_parts.append(f"  - 目标CPA(代码已算): ${_cpa:.2f}")
        _aov = strategy_context.get("avg_order_value")
        if _aov is not None:
            ctx_parts.append(f"  - 平均订单金额(代码已算): ${_aov:.2f}")
        if inv_days is not None:
            ctx_parts.append(
                f"  - 加预算前提·库存门槛: {'✅ 库存≥30天' if inv_days >= 30 else '⚠ 库存<30天，不得加预算'}"
            )
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
        guardrail_instruction = (strategy_context.get("_guardrail_instruction") or "").strip()
        if guardrail_instruction:
            ctx_parts.append(guardrail_instruction)
            ctx_parts.append("")

        # 构建活动列表
        camp_parts = ["## 活动列表 (逐活动分析)"]
        # cid：批内短句柄（C1..CN）。代码在拼 prompt 时即建立 cid→summary 映射，
        # LLM 只需把 cid 原样回吐，代码据此回填权威 campaign_key 等字段（根治长串抄错漂移）。
        cid_map: dict[str, dict] = {}
        for i, s in enumerate(campaign_summaries):
            cid = f"C{i + 1}"
            cid_map[cid] = s
            camp_parts.append(f"\n### 活动 {cid}: {s.get('campaign_name', '?')}")
            alert = s.get("_guardrail_alert")
            if alert:
                camp_parts.append(f"  ⚠️ 护栏告警（内部复判材料，不得写入 reason/evidence）: {alert}")
            camp_parts.append(f"  - 句柄 cid: {cid}（输出 JSON 的 cid 字段须原样回填此值）")
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
            if s.get("budget_utilization_tier"):
                camp_parts.append(f"  - 预算资格档位(代码判定): {s['budget_utilization_tier']}")
            if "budget_increase_eligible" in s:
                camp_parts.append(f"  - 加预算资格(代码判定): {'✅ 表现好才可加预算' if s['budget_increase_eligible'] else '❌ 不可加预算'}")
            # 组合预算瓶颈感知 (KB23 §8.4)
            if s.get("_portfolio_group") and s.get("_portfolio_utilization") is not None:
                _pg = s["_portfolio_group"]
                _pu = s["_portfolio_utilization"]
                _pb = s.get("_portfolio_budget")
                _pb_str = f"${_pb:.0f}" if _pb is not None else "?"
                camp_parts.append(
                    f"  - ⚠️ 所属组合: {_pg} | 组合日预算: {_pb_str} | "
                    f"组合7天利用率: {_pu:.0%}"
                )
                if _pu >= 1.0 and s.get("_sample_insufficient", False):
                    camp_parts.append(
                        f"     → ⚠️ 组合预算瓶颈(利用率{_pu:.0%}) + 本活动样本不足(cost<5或clicks<10)，"
                        "禁止调 bid/预算、禁止淘汰，action=keep。"
                    )
            # 广告位加价比例 (KB 19 §5 决策矩阵 — 来自 basic_info，非 placement_report)
            ppcts = s.get("_placement_pcts", {})
            if ppcts:
                camp_parts.append(f"  - 当前加价比例: 头部:{ppcts.get('头部',0)}%, "
                                 f"商品:{ppcts.get('商品',0)}%, 其他:{ppcts.get('其他',0)}%")
                # 各广告位真实出价 = Bid×(1+加价比例)。调 bid 与广告位加价会叠加，
                # 分析建议须以真实出价评估幅度，勿只看基础 Bid。
                _cbid = s.get("current_bid", 0) or 0
                def _real_bid(_pct):
                    try:
                        return round(float(_cbid) * (1 + (float(_pct) or 0) / 100), 3)
                    except (TypeError, ValueError):
                        return _cbid
                camp_parts.append(
                    f"  - 当前真实出价(Bid×(1+加价比例)): "
                    f"头部 ${_real_bid(ppcts.get('头部',0))}, "
                    f"商品 ${_real_bid(ppcts.get('商品',0))}, "
                    f"其他 ${_real_bid(ppcts.get('其他',0))}"
                )
            # 自然排名（周排名，仅精准；三态：有排名/已掉榜/无数据不渲染）
            nr = s.get("_natural_rank")
            if nr:
                cur, near, chg = nr.get("cur"), nr.get("near"), nr.get("change")
                if cur is not None:
                    arrow = ""
                    if chg is not None:
                        sym = "↑" if chg > 0 else ("↓" if chg < 0 else "持平")
                        arrow = f"（较上次 {sym}{abs(chg) if chg else ''}）"
                    camp_parts.append(f"  - 自然排名: 第{cur}位{arrow}")
                elif near is not None:
                    camp_parts.append(f"  - 自然排名: ⚠️已掉榜（上次第{near}位）")
            # 广告位懒加载数据 (KB 22 §2.3 / KB 19 §5)
            if s.get("_placement_data"):
                pd_data = s["_placement_data"]
                camp_parts.append(f"  - ★广告位表现 (per-placement):")
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
                terms = st_data if isinstance(st_data, list) else st_data.get("terms", st_data.get("search_terms", []))
                status = st_data.get("summary", {}).get("search_term_fetch_status") if isinstance(st_data, dict) else None
                if status:
                    camp_parts.append(f"  - 搜索词取数状态: {status}（不生成搜索词来源动作）")
                elif terms:
                    camp_parts.append(f"  - ★搜索词报告（7d 基线，{len(terms)} 个入选词）:")
                    camp_parts.append("    字段口径：每行窗口独立；term_sample_insufficient 仅表示该词 7d 样本不足；14d 只作辅助事实，不能替代 7d 动作基线。")
                    camp_parts.extend(render_search_term_prompt_lines(st_data))
            elif s.get("search_term_fetch_status"):
                camp_parts.append(
                    f"  - 搜索词取数状态: {s['search_term_fetch_status']}（活动样本不足，仅观察；"
                    "禁止生成搜索词来源的否词或提精准动作）"
                )

        # 策略总览(执行总纲)preamble：非空时置于用户消息最前，作为本批逐活动判断的统一框架
        overview_text = (strategy_context.get("_strategic_overview_text") or "").strip()
        preamble = (
            f"## 今日执行总纲（逐活动判断须遵循此宏观框架）\n{overview_text}\n\n---\n\n"
            if overview_text else ""
        )

        user_message = preamble + "\n".join(ctx_parts) + "\n" + "\n".join(camp_parts)

        messages = [
            {"role": "system", "content": _build_campaign_system_prompt(
                task_type, strategy_context.get("ad_directions"),
            )},
            {"role": "user", "content": user_message},
        ]

        try:
            raw = await self.client.chat(
                messages=messages,
                temperature=temperature,
                response_format={"type": "json_object"},
                max_tokens=8192,
                timeout_override=timeout_override,
                label="campaign_batch",
            )
            parsed = self._parse_json(raw)
            raw_adjustments = parsed.get("campaign_adjustments", [])

            def _norm_search_term(value: object) -> str:
                return " ".join(str(value or "").strip().lower().split())

            # broad 的否词和提精准共享同一份权威词索引：只包含本次实际
            # 注入 LLM 的 7d 基线词。取数跳过/失败、空报告、被过滤或截断的词
            # 均不在索引中，因而不能生成搜索词来源动作。
            search_terms_by_cid: dict[str, dict[str, dict]] = {}
            if task_type == "broad":
                for cid, src in cid_map.items():
                    raw_terms = src.get("_search_term_data") or []
                    status = str(src.get("search_term_fetch_status") or "").strip()
                    if isinstance(raw_terms, dict):
                        summary = raw_terms.get("summary") or {}
                        status = str(summary.get("search_term_fetch_status") or status).strip()
                        raw_terms = raw_terms.get("terms", raw_terms.get("search_terms")) or []
                    indexed: dict[str, dict] = {}
                    if not status:
                        for term in raw_terms:
                            if not isinstance(term, dict):
                                continue
                            normalized = _norm_search_term(term.get("keyword"))
                            if normalized:
                                indexed[normalized] = term
                    search_terms_by_cid[cid] = indexed

            # cid 回填：用代码持有的 cid_map 把 LLM 回吐的句柄解析回权威结构/现状字段，
            # 无条件覆盖 LLM 任何值（根治 LLM 抄错 campaign_key / 误报 current_*）。
            # 命中失败（越界/幻觉/重复 cid）→ 丢弃该条 → 该活动按"缺失"处理，由外层护栏补答循环补答。
            adjustments = []
            seen_cids: set[str] = set()
            for adj in raw_adjustments:
                if not isinstance(adj, dict):
                    continue
                cid = str(adj.get("cid", "")).strip()
                src = cid_map.get(cid)
                if src is None or cid in seen_cids:
                    logger.warning("Campaign batch [%s] 丢弃无效 cid=%r (越界/幻觉/重复)", asin, cid)
                    continue
                seen_cids.add(cid)
                adj["campaign_key"] = src.get("campaign_key", "")
                adj["campaign_name"] = src.get("campaign_name", "")
                adj["child_asin"] = src.get("child_asin", "")
                adj["keyword_text"] = src.get("keyword_text", "")
                adj["match_type"] = src.get("match_type", "")
                adj["current_bid"] = src.get("current_bid")
                adj["current_budget"] = src.get("current_budget")
                # 否词闸门：仅保留精准否定，词组否定在本层丢弃，下游永无感知
                raw_neg = adj.get("negative_keywords") or []
                cleaned_neg = []
                _neg_total = len(raw_neg)
                _neg_dropped_not_exact = 0
                _neg_dropped_not_in_data = 0
                for nk in raw_neg:
                    if not isinstance(nk, dict):
                        continue
                    nk_mt = str(nk.get("match_type", "")).strip().upper()
                    if nk_mt != "NEGATIVE_EXACT":
                        _neg_dropped_not_exact += 1
                        logger.warning(
                            "Campaign batch [%s] cid=%s 丢弃非精准否词 keyword=%r match_type=%r",
                            asin, cid, nk.get("keyword"), nk_mt,
                        )
                        continue
                    keyword = str(nk.get("keyword") or "").strip()
                    if task_type == "broad":
                        authoritative = search_terms_by_cid.get(cid, {}).get(
                            _norm_search_term(keyword)
                        )
                        if authoritative is None:
                            _neg_dropped_not_in_data += 1
                            logger.warning(
                                "Campaign batch [%s] cid=%s 丢弃非本轮搜索词否词 keyword=%r",
                                asin, cid, keyword,
                            )
                            continue
                        keyword = str(authoritative.get("keyword") or keyword).strip()
                    cleaned_neg.append({
                        "keyword": keyword,
                        "match_type": "NEGATIVE_EXACT",
                        "reason": (nk.get("reason") or "")[:512],
                    })
                if _neg_total > 0:
                    logger.info(
                        "Campaign batch [%s] cid=%s 否词闸门: LLM产出=%d 通过=%d 丢弃非EXACT=%d 丢弃非搜索词=%d",
                        asin, cid, _neg_total, len(cleaned_neg), _neg_dropped_not_exact, _neg_dropped_not_in_data,
                    )
                adj["negative_keywords"] = cleaned_neg
                adjustments.append(adj)
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

            # 广泛流额外吐出搜索词提精准候选；cid 与搜索词均由本层校验/回填。
            # 词根由独立调用点 recommend_keyword_roots 统一产出，不在本层处理。
            promotion_candidates: list[dict] = []
            if task_type == "broad":
                seen_pairs: set[tuple[str, str]] = set()
                for raw_candidate in parsed.get("exact_promotion_candidates") or []:
                    if not isinstance(raw_candidate, dict):
                        continue
                    cid = str(raw_candidate.get("cid") or "").strip()
                    src = cid_map.get(cid)
                    search_term = _norm_search_term(raw_candidate.get("search_term"))
                    term = search_terms_by_cid.get(cid, {}).get(search_term)
                    if src is None or term is None:
                        logger.warning(
                            "Campaign batch [%s] 丢弃无效提精准候选 cid=%r term=%r",
                            asin, cid, raw_candidate.get("search_term"),
                        )
                        continue
                    relevance_tier = str(raw_candidate.get("relevance_tier") or "").strip().upper()
                    if relevance_tier != "R1":
                        continue
                    pair = (cid, search_term)
                    if pair in seen_pairs:
                        continue
                    seen_pairs.add(pair)
                    candidate = SearchTermPromotionCandidate(
                        campaign_key=str(src.get("campaign_key") or ""),
                        campaign_name=str(src.get("campaign_name") or ""),
                        campaign_match_type=str(src.get("match_type") or ""),
                        search_term=str(term.get("keyword") or search_term).strip(),
                        keyword_class=str(raw_candidate.get("keyword_class") or "").strip().lower(),
                        relevance_tier=relevance_tier,
                        reason=humanize_ops_text(self._sanitize_ops_text(
                            raw_candidate.get("reason") or "",
                        )),
                        evidence=[humanize_ops_text(self._sanitize_ops_text(item))
                                  for item in (raw_candidate.get("evidence") or []) if item],
                        clicks=int(term.get("clicks") or 0),
                        orders=int(term.get("orders") or 0),
                        cost=float(term.get("cost") or 0.0),
                        sales=float(term.get("sales") or 0.0),
                    )
                    promotion_candidates.append(candidate.model_dump())
            parsed["exact_promotion_candidates"] = promotion_candidates
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

    # ── Campaign 新增活动 (KB 16 + 06) ──────────────────────────────────────
    async def recommend_new_campaigns(
        self,
        asin: str,
        candidates: list[dict],
        strategy_context: dict,          # ctx_dict, 含 _strategic_overview_text(posture_brief)
        temperature: float = 0.3,
        timeout_override: float | None = None,
        *,
        product_title: str = "",
        existing_keywords: list[str] | None = None,   # 已投放关键词（相关性参照锚点）
        listing_info: dict | None = None,              # {bullets, category}（来自 erp_listing_product_info）
        colors: list[str] | None = None,               # 去重归一化的产品颜色列表
        today_date: str = "",                          # 站点当地时间 YYYY-MM-DD
    ) -> dict:
        """KB 16+06 新增活动批量分析（单批）。LLM 只判 action + keyword_class + 文本，数值代码定。

        与 recommend_campaign_batch 同结构返回:
          {"parsed": dict, "raw_output": str, "success": bool, "error": str, "temperature": float}
        """
        logger.info("Campaign new LLM 入口 [%s] items=%d temp=%.2f",
                    asin, len(candidates), temperature)

        # 策略上下文段（精简，仅 LLM 判选词/类别所需）
        ctx_parts = [
            "## 策略上下文 (ASIN 级，全批共享)",
            # [产品阶段] 已由经营模式替代，不再注入 LLM prompt
            # f"  - 产品阶段: {strategy_context.get('product_stage', '?')}",
            f"  - 产品定位: {product_level_with_code(strategy_context.get('product_level', '')) or '?'}",
            f"  - 淡旺季: {strategy_context.get('season_stage', '?')}",
            f"  - 经营模式: {strategy_context.get('operating_mode')}",
            f"  - 广告目的: {strategy_context.get('ad_purposes', [])}",
            f"  - 目标关键词类型: {strategy_context.get('target_keyword_strategy', [])}",
        ]
        if strategy_context.get("target_acos"):
            ctx_parts.append(f"  - 运营目标 ACOS: {strategy_context['target_acos']}%")
        _adirs = strategy_context.get("ad_directions") or []
        if _adirs:
            ctx_parts.append(f"  - 广告方向(运营已选): {_adirs}")

        # 相关性锚点：产品标题 + 已投放关键词（去 brand/category：品类太粗、会把 LLM 引向品类级误匹配，
        # 判别"短裙≠中长裙"靠标题的具体属性；brand 仅 KB06 品牌词识别用，不作相关性锚点）
        anchor_parts = ["## 本产品标题 + 已投放关键词（相关性参照）"]
        if (product_title or "").strip():
            anchor_parts.append(f"  - 产品标题: {product_title.strip()[:300]}")
        _exist = [k for k in (existing_keywords or []) if str(k).strip()][:60]
        if _exist:
            anchor_parts.append(f"  - 已投放关键词({len(_exist)}个，相关性参照): {', '.join(_exist)}")
        else:
            anchor_parts.append("  - 已投放关键词: 无（新品/小 ASIN，锚点稀薄 → 以标题为主判相关性，勿过度 skip）")

        # 五点 + 类目（相关性锚点，来自 Listing 后台 erp_listing_product_info）
        if listing_info and isinstance(listing_info, dict):
            attr_parts = ["## 产品五点 + 类目（相关性锚点，来自 Listing 后台）"]
            bullets = listing_info.get("bullets") or []
            if bullets:
                attr_parts.append("  - 五点:")
                for i, b in enumerate(bullets, 1):
                    attr_parts.append(f"    {i}. {b}")
            cat = listing_info.get("category") or ""
            if cat:
                attr_parts.append(f"  - 类目: {cat}")
            anchor_parts.append("")
            anchor_parts.extend(attr_parts)

        # 产品颜色 + 当前日期
        extra_parts = []
        if colors:
            extra_parts.append(f"## 产品可用颜色（来自 Listing 变体）")
            extra_parts.append(f"  {', '.join(colors)}")
        if today_date:
            extra_parts.append(f"## 当前日期（站点当地时间）")
            extra_parts.append(f"  {today_date}")

            # 计算距离主要节日的天数（纯提示，不计算具体日期差）
            extra_parts.append(f"  主要节日参考：")
            extra_parts.append(f"    - Halloween（10月31日）")
            extra_parts.append(f"    - Christmas（12月25日）")
            extra_parts.append(f"    - Valentine's Day（2月14日）")
            extra_parts.append(f"    - Easter（春分月圆后第一个周日）")
            extra_parts.append(f"    - Mother's Day（5月第二个周日）")
            extra_parts.append(f"    - Father's Day（6月第三个周日）")
            extra_parts.append(f"    - Black Friday/Cyber Monday（11月感恩节后）")
            extra_parts.append(f"    - Prime Day（通常在7月）")
            extra_parts.append(f"    - Back to School（8-9月）")
        if extra_parts:
            anchor_parts.append("")
            anchor_parts.extend(extra_parts)

        # 今日执行总纲 preamble（与 batch 流一致，注入 posture_brief）
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

        messages = [
            {"role": "system", "content": _build_new_campaign_prompt()},
            {"role": "user", "content": user_message},
        ]
        import json as _json

        last_error = ""
        for attempt in (1, 2):
            try:
                raw = await self.client.chat(
                    messages=messages,
                    temperature=temperature,
                    response_format={"type": "json_object"},
                    max_tokens=8192,   # 40 候选词 + KB prompt 输出长 JSON，4096 偏紧易截断
                    timeout_override=timeout_override,
                    label="new_campaign",
                )
                parsed = self._parse_json(raw)
                if not isinstance(parsed, dict):
                    raise ValueError("解析结果非 dict")
                # 文风清洗 reason/evidence（复用 batch 流相同处理）
                for it in parsed.get("new_campaigns", []) or []:
                    if isinstance(it, dict):
                        it["reason"] = humanize_ops_text(self._sanitize_ops_text(it.get("reason", "")))
                        it["evidence"] = [
                            humanize_ops_text(self._sanitize_ops_text(e))
                            for e in (it.get("evidence", []) or [])
                        ]
                logger.info("Campaign new LLM 成功 [%s] attempt=%d items=%d",
                            asin, attempt, len(parsed.get("new_campaigns", []) or []))
                return {"parsed": parsed, "raw_output": raw, "success": True,
                        "error": "", "temperature": temperature}
            except _json.JSONDecodeError as e:
                last_error = f"{type(e).__name__}: {e}"
                if attempt == 1:
                    logger.warning("Campaign new JSON 解析失败 [%s] attempt=1，重试一次: %s", asin, last_error)
                else:
                    logger.warning("Campaign new JSON 解析失败 [%s] attempt=2，放弃: %s", asin, last_error)
            except Exception as e:
                logger.warning("recommend_new_campaigns 异常 [%s]: %s", asin, e)
                return {"parsed": {}, "raw_output": "", "success": False,
                        "error": f"{type(e).__name__}: {e}", "temperature": temperature}
        return {"parsed": {}, "raw_output": "", "success": False,
                "error": last_error, "temperature": temperature}

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
            # [产品阶段] 已由经营模式替代，不再注入 LLM prompt
            # f"  - 产品阶段: {strategy_context.get('product_stage', '?')}",
            f"  - 产品定位: {product_level_with_code(strategy_context.get('product_level', '')) or '?'}",
            f"  - 淡旺季: {strategy_context.get('season_stage', '?')}",
            f"  - 广告目的: {strategy_context.get('ad_purposes', [])}",
            f"  - 广告方向(运营已选): {strategy_context.get('ad_directions', []) or '未选'}",
            f"  - 目标关键词类型: {strategy_context.get('target_keyword_strategy', [])}",
        ]
        ctx_lines.append(f"  - 经营模式: {strategy_context.get('operating_mode')}")
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

        from app.config.settings import settings  # 强档(pro+思考)配置
        try:
            raw = await self.client.chat(
                messages=messages,
                temperature=temperature,
                response_format={"type": "json_object"},
                max_tokens=8192,
                timeout_override=max(timeout_override or 0, 180),   # 强档慢，至少 180s
                model=settings.llm_model_strong,                    # 总览=重要节点→强档
                thinking=True,
                reasoning_effort=settings.llm_strong_reasoning_effort,
                label="campaign_overview",
            )
            parsed = self._parse_json(raw)
            raw_growth_flag = parsed.get("allow_growth_analysis", True)
            # 严格类型：只接受 JSON boolean；字符串 "false"/数字/缺失一律 fail-open=True
            allow_growth = raw_growth_flag if isinstance(raw_growth_flag, bool) else True
            out = {
                "assessment_text": self._sanitize_ops_text(parsed.get("assessment", "") or ""),
                "direction_text": self._sanitize_ops_text(parsed.get("direction", "") or ""),
                "posture_brief": self._sanitize_ops_text(parsed.get("posture_brief", "") or ""),
                "allow_growth_analysis": allow_growth,
            }
            logger.info("Campaign overview 成功 [%s]", asin)
            return out
        except Exception as e:
            logger.warning("Campaign overview 失败 [%s]: %s", asin, e)
            return {"error": str(e)}

    # ── 搜索词根提取 ──────────────────────────────────────────────────
    async def recommend_keyword_roots(
        self,
        parent_asin: str,
        search_terms: list[str],
        *,
        temperature: float = 0.1,
        timeout_override: float | None = None,
    ) -> dict[str, str]:
        """一次 LLM 调用为所有去重搜索词提取关键词根。

        返回 {search_term: keyword_root}。LLM 做语义匹配——拼写变体/词序差异归一到同一词根。
        """
        if not search_terms:
            return {}

        terms_text = "\n".join(f"{i + 1}. {t}" for i, t in enumerate(search_terms))
        kb_rules = kb.build("keyword_roots")
        prompt = (
            "你是搜索词根提取助手。为每个搜索词提取关键词根（keyword root）。\n\n"
            "## 业务背景\n"
            + kb_rules +
            "\n\n## 词根提取规则\n"
            "词根 = 去掉修饰成分后的核心产品词（1-3个词）。\n"
            "修饰成分包括：颜色（black/red）、尺寸（plus size/xxl）、风格（sexy/cute）、\n"
            "节日（halloween/christmas）、人群（women/men/kids）、材质（cotton/lace）、\n"
            "活动词（buy/cheap/sale/deal）、意图词（for women/near me）。\n\n"
            "## 示例（按品类）\n"
            "### 内衣/bra\n"
            "- strapless bra / straples bra / bra strapless → \"strapless bra\"（词序差异归一）\n"
            "- push up bra / pushup bra / push up bras → \"push up bra\"（拼写变体）\n"
            "- lace bra / lacy bra / lace push up bra → \"lace bra\" 或 \"bra\"（材质修饰可剥）\n"
            "- sports bra / sport bra / sports bras for women → \"sports bra\"\n"
            "- sticky bra / adhesive bra / backless sticky bra → \"sticky bra\"（同义归并）\n"
            "- strapless bra adhesive / sticky bra nude → 归入 \"strapless bra\" / \"sticky bra\"\n"
            "- plus size bra / large bra / full figured bra → \"bra\"（人群/尺寸修饰剥除）\n\n"
            "### 泳装/bikini\n"
            "- black bikini / sexy bikini / cute bikini → 全部 \"bikini\"\n"
            "- bikni / bikinny / bikin → \"bikini\"（拼写容错）\n"
            "- bikini for women / women bikini / womens bikini → \"bikini\"\n"
            "- high waist bikini / high waisted bikini → \"high waist bikini\"\n"
            "- bikini top / bikini bottom / bikini set → \"bikini top\" / \"bikini bottom\" / \"bikini set\"\n"
            "- tankini / tankini swimsuit / tankini top → \"tankini\"\n"
            "- swimsuit / swim suit / bathing suit / one piece swimsuit → \"swimsuit\"\n"
            "- monokini / cut out swimsuit → \"monokini\"\n\n"
            "### 裙子/dress\n"
            "- bodycon dress / bodycon mini dress / bodycon → \"bodycon dress\"\n"
            "- mini dress / mini dresses / short dress → \"mini dress\"\n"
            "- midi dress / midi dresses / mid length dress → \"midi dress\"\n"
            "- maxi dress / long dress / floor length dress → \"maxi dress\"\n"
            "- slip dress / silk slip dress / satin slip dress → \"slip dress\"\n"
            "- wrap dress / wrap mini dress → \"wrap dress\"\n"
            "- sweater dress / knit dress → \"sweater dress\"\n"
            "- cocktail dress / party dress → \"cocktail dress\"（注意：party 太泛不纳入根）\n"
            "- velvet dress / sequin dress / lace dress → \"dress\"（材质修饰剥除）\n"
            "- summer dress / spring dress / casual dress → \"dress\"（季节/场合剥除）\n\n"
            "### 上衣/top\n"
            "- crop top / cropped top / crop tops → \"crop top\"\n"
            "- tank top / tank tops / ribbed tank top → \"tank top\"\n"
            "- bodysuit / body suit / bodysuits → \"bodysuit\"\n"
            "- lace top / lace blouse → \"lace top\" 或 \"top\"\n"
            "- tube top / strapless top / bandeau top → \"tube top\"（同义归并优先选最常见名）\n"
            "- corset top / corset / lace corset top → \"corset top\"\n"
            "- off shoulder top / off the shoulder top → \"off shoulder top\"\n\n"
            "### 下装/bottom\n"
            "- leggings / legging / leggins → \"leggings\"\n"
            "- yoga pants / yoga leggings / workout leggings → \"yoga pants\"（同功能归并）\n"
            "- leather pants / leather legging → \"leather pants\"\n"
            "- shorts / short pants / denim shorts → \"shorts\"\n"
            "- skirt / mini skirt / midi skirt / pleated skirt → \"skirt\"\n"
            "- tennis skirt / golf skirt / athletic skirt → \"tennis skirt\"\n\n"
            "### 丝袜/hosiery\n"
            "- fishnet stocking / fishnet tights / fishnets → \"fishnet\"\n"
            "- stocking / stockings / thigh high stocking → \"stocking\"\n"
            "- pantyhose / panty hose / tights → \"pantyhose\" 或 \"tights\"\n"
            "- knee high sock / knee high socks → \"knee high sock\"\n\n"
            "### 连体衣/jumpsuit\n"
            "- jumpsuit / jump suit / jumpsuits for women → \"jumpsuit\"\n"
            "- romper / rompers / romper jumpsuit → \"romper\"\n"
            "- overalls / overall / denim overalls → \"overalls\"\n\n"
            "### 睡衣/lingerie\n"
            "- lingerie / sexy lingerie / lace lingerie → \"lingerie\"\n"
            "- babydoll / baby doll / babydoll lingerie → \"babydoll\"\n"
            "- chemise / silk chemise / lace chemise → \"chemise\"\n"
            "- teddy / teddy lingerie / lace teddy → \"teddy\"\n"
            "- garter belt / garter / suspender belt → \"garter belt\"\n\n"
            "## 注意\n"
            "- 优先保留多词核心（\"push up bra\" 而非 \"bra\"；\"crop top\" 而非 \"top\"）——仅当修饰成分明显时才剥。\n"
            "- 不确定是否为核心时宁可多保留一个词，不要过度截断。\n"
            "- 无法提取 → 填空字符串 \"\"。\n\n"
            "输出 JSON（严格 schema）：{\"roots\": [{\"term_index\": 1, \"root\": \"bikini\"}, ...]}"
        )
        from app.config.settings import settings

        user_msg = "## 搜索词列表\n" + terms_text
        messages = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": user_msg},
        ]

        timeout = max(timeout_override or 0, settings.llm_timeout)
        try:
            raw = await self.client.chat(
                messages=messages,
                temperature=temperature,
                response_format={"type": "json_object"},
                max_tokens=4096,
                timeout_override=timeout,
                label="keyword_roots",
            )
            parsed = self._parse_json(raw)
            root_items = parsed.get("roots", []) if isinstance(parsed, dict) else []
            result: dict[str, str] = {}
            for item in root_items:
                if not isinstance(item, dict):
                    continue
                idx = item.get("term_index")
                root = str(item.get("root") or "").strip()
                if isinstance(idx, int) and 1 <= idx <= len(search_terms) and root:
                    term = search_terms[idx - 1]
                    # 校验：root 必须是 search_term 的连续子序列
                    root_tokens = root.split()
                    term_tokens = term.split()
                    w = len(root_tokens)
                    if any(term_tokens[i:i + w] == root_tokens for i in range(len(term_tokens) - w + 1)):
                        result[term] = root
            logger.info(
                "keyword_roots [%s]: %d terms → %d roots",
                parent_asin, len(search_terms), len(set(result.values())),
            )
            return result
        except Exception as e:
            logger.warning("keyword_roots LLM 失败 [%s]: %s", parent_asin, e)
            return {}

    # ── 核心词语义判定 ──────────────────────────────────────────────────
    async def recommend_semantic_core(
        self,
        parent_asin: str,
        listing: dict,
        keywords: list[str],
        *,
        temperature: float = 0.3,
        timeout_override: float | None = None,
    ) -> dict:
        """一次 LLM 调用批量判断所有关键词的 semantic_conflict + semantic_core。

        listing: {"title", "bullets_str", "variants_str", "category"}
        keywords: 关键词文本列表（≤30）
        返回: {"keywords": [...], "error": ""} 或 {"error": "..."}
        """
        from app.config.settings import settings  # noqa: F811

        kw_lines = "\n".join(f"{i+1}. {kw}" for i, kw in enumerate(keywords))
        product_lines = [
            f"- 标题: {listing.get('title', '')}",
            f"- 五点: {listing.get('bullets_str', '')}",
            f"- 变体: {listing.get('variants_str', '')}",
            f"- 类目: {listing.get('category', '')}",
        ]
        user_message = "## 产品信息\n" + "\n".join(product_lines) + "\n\n## 待判断关键词\n" + kw_lines

        messages = [
            {"role": "system", "content": _build_semantic_core_prompt()},
            {"role": "user", "content": user_message},
        ]

        timeout = max(timeout_override or 0, settings.core_keyword_llm_timeout)
        try:
            raw = await self.client.chat(
                messages=messages,
                temperature=temperature,
                response_format={"type": "json_object"},
                max_tokens=4096,
                timeout_override=timeout,
                label="semantic_core",
            )
            parsed = self._parse_json(raw)
            kw_list = parsed.get("keywords", [])
            return {"keywords": kw_list, "error": ""}
        except Exception as e:
            logger.warning("semantic_core LLM 失败 [%s]: %s", parent_asin, e)
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

        # 精简单条信息，控制输入 token（synthesis 只需语义聚类，不需要数值和元数据）
        compact: list[dict] = []
        for adj in adjustments:
            d = adj.model_dump() if hasattr(adj, "model_dump") else dict(adj)
            compact.append({
                "campaign_key": d.get("campaign_key", ""),
                "campaign_name": (d.get("campaign_name") or "")[:60],
                "action": d.get("action", ""),
                "triggered_rule": d.get("triggered_rule", ""),
                "match_type": d.get("match_type", ""),
                "reason": d.get("reason") or "",  # 完整理由：分组命脉，不截断
            })

        ctx_lines = [
            # [产品阶段] 已由经营模式替代，不再注入 LLM prompt
            # f"  - 产品阶段: {strategy_context.get('product_stage', '?')}",
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

        from app.config.settings import settings  # 强档(pro+思考)配置
        try:
            raw = await self.client.chat(
                messages=messages,
                temperature=temperature,
                response_format={"type": "json_object"},
                max_tokens=8192,
                timeout_override=max(timeout_override or 0, 240),   # 强档慢，至少 240s
                model=settings.llm_model_strong,                    # 汇总=重要节点→强档
                # 不传 thinking：synthesis 是语义聚类不是复杂推理；思考吃 token 会导致 JSON 截断
                label="campaign_synthesis",
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

    async def recommend_budget_reallocation(
        self,
        asin: str,
        agg: dict,
        *,
        temperature: float = 0.3,
        timeout_override: float | None = None,
    ) -> dict:
        """KB23 预算回算：输入代码预聚合包 agg，输出 3 组推荐值 + 父净增。

        算术已在 agg 算好，LLM 只判分配方式(§7)+组内优先级(§3.1A)+解释。
        失败返回 {"error": "..."}，调用方据此回落规则引擎。
        """
        from app.config.settings import settings

        ng = len(agg.get("groups", []))
        logger.info(
            "Campaign budget realloc LLM 入口 [%s] groups=%d available=%s timeout=%ss",
            asin, ng, agg.get("parent", {}).get("available_for_increase"),
            timeout_override or settings.campaign_budget_agent_timeout,
        )

        messages = [
            {"role": "system", "content": _build_budget_realloc_prompt()},
            {"role": "user", "content": json.dumps(agg, ensure_ascii=False)},
        ]
        try:
            raw = await self.client.chat(
                messages=messages,
                temperature=temperature,
                response_format={"type": "json_object"},
                max_tokens=8192,
                timeout_override=max(timeout_override or 0, settings.campaign_budget_agent_timeout),
                model=settings.llm_model_strong,
                thinking=True,
                reasoning_effort=settings.llm_strong_reasoning_effort,
                label="budget_realloc",
            )
            parsed = self._parse_json(raw)
            logger.info(
                "Campaign budget realloc 成功 [%s] method=%s groups=%d",
                asin, parsed.get("allocation_method"), len(parsed.get("budget_groups", []) or []),
            )
            return parsed
        except Exception as e:
            logger.warning("Campaign budget realloc 失败 [%s]: %s", asin, e)
            return {"error": str(e)}


# 全局单例
reasoner = LLMReasoner()

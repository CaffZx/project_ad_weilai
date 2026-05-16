"""POST /llm/report — 调用 DeepSeek 生成综合分析报告"""

from fastapi import APIRouter, Depends

from app.api.deps import get_llm_reasoner
from app.llm.reasoner import LLMReasoner
from app.models.decision import LLMReportRequest, LLMReportResponse

router = APIRouter()


@router.post("/llm/report", response_model=LLMReportResponse)
async def llm_report(
    req: LLMReportRequest,
    reasoner: LLMReasoner = Depends(get_llm_reasoner),
):
    """基于规则引擎结果 + LLM 生成综合分析报告

    接收数据摘要、方向评分、规则校验结果、决策包，
    调用 DeepSeek 生成自然语言综合分析。
    """
    analysis = await reasoner.analyze(
        asin=req.asin,
        data_summary=req.data_summary,
        scores=req.scores,
        validations=req.validations,
        decisions=req.decisions,
    )

    return LLMReportResponse(
        asin=req.asin,
        overall_analysis=analysis.get("overall_analysis", ""),
        direction_analyses=analysis.get("direction_analyses", []),
        action_priorities=analysis.get("action_priorities", []),
        risk_warnings=analysis.get("risk_warnings", []),
    )

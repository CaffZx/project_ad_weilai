"""Layer 1.4 执行层 API — 广告方向选择 + P3 上游推荐"""

from fastapi import APIRouter, Depends

from app.api.deps import get_workflow_orchestrator
from app.core.workflow_orchestrator import WorkflowOrchestrator
from app.models.layers import (
    ExecutionOptionsResponse,
    ExecutionSelectRequest,
    ExecutionSelectResponse,
    TargetAcosRequest,
    TargetAcosRecommendation,
    TargetAcosOverrideRequest,
    BudgetBidRequest,
    BudgetBidRecommendation,
    BudgetOverrideRequest,
    UnifiedRecommendRequest,
    UnifiedRecommendResponse,
)

router = APIRouter()


@router.post("/execution/options", response_model=ExecutionOptionsResponse)
async def execution_options(
    req: dict,
    orchestrator: WorkflowOrchestrator = Depends(get_workflow_orchestrator),
):
    """获取执行层方向选项（含AI推荐）

    Agent 基于上游战略+策略+诊断信息推荐广告方向。
    各方向非互斥，可多选。不持久化为长期配置。
    """
    return await orchestrator.get_execution_options(req.get("asin", ""), days=req.get("days", 7))


@router.post("/execution/select", response_model=ExecutionSelectResponse)
async def execution_select(
    req: ExecutionSelectRequest,
    orchestrator: WorkflowOrchestrator = Depends(get_workflow_orchestrator),
):
    """确认执行层方向选择（持久化至 workflow_state，Campaign 分析复用）"""
    return await orchestrator.confirm_execution(req)


# ── P3 上游推荐端点 ──────────────────────────────────────


@router.post("/execution/target-acos", response_model=TargetAcosRecommendation)
async def target_acos_recommendation(
    req: TargetAcosRequest,
    orchestrator: WorkflowOrchestrator = Depends(get_workflow_orchestrator),
):
    """P3 Feature 1: 目标 ACOS 推荐

    基于产品阶段、广告目的、毛利率、ACOS趋势、自然单占比的7步规则链。
    纯算法驱动，不依赖LLM。输出5%粒度的ACOS目标值（≥5%且<40%）。
    """
    return await orchestrator.get_target_acos_recommendation(req.asin, days=req.days)


@router.post("/execution/budget-bid", response_model=BudgetBidRecommendation)
async def budget_bid_recommendation(
    req: BudgetBidRequest,
    orchestrator: WorkflowOrchestrator = Depends(get_workflow_orchestrator),
):
    """P3 Feature 2: 预算和 Bid 推荐

    基于产品阶段、淡旺季、花费率、关键词Bid/CPC偏差、历史调整记录的5步规则链。
    纯算法驱动，不依赖LLM。
    """
    return await orchestrator.get_budget_bid_recommendation(req.asin, days=req.days)


# ── P3 运营手动覆盖端点 ─────────────────────────────────


@router.post("/execution/target-acos/override", response_model=dict)
async def save_target_acos_override(
    req: TargetAcosOverrideRequest,
    orchestrator: WorkflowOrchestrator = Depends(get_workflow_orchestrator),
):
    """运营手动设定目标 ACOS，写入 long_term_config 长期保存"""
    ok = orchestrator.save_target_acos_override(req.asin, req.value)
    return {"asin": req.asin, "saved": ok, "value": req.value}


@router.delete("/execution/target-acos/override", response_model=dict)
async def clear_target_acos_override(
    req: TargetAcosRequest,
    orchestrator: WorkflowOrchestrator = Depends(get_workflow_orchestrator),
):
    """清除手动设定的目标 ACOS，恢复算法推荐"""
    ok = orchestrator.clear_target_acos_override(req.asin)
    return {"asin": req.asin, "cleared": ok}


@router.post("/execution/budget-bid/override", response_model=dict)
async def save_budget_override(
    req: BudgetOverrideRequest,
    orchestrator: WorkflowOrchestrator = Depends(get_workflow_orchestrator),
):
    """运营手动设定日预算，写入 long_term_config 长期保存"""
    ok = orchestrator.save_budget_override(req.asin, req.value)
    return {"asin": req.asin, "saved": ok, "value": req.value}


@router.delete("/execution/budget-bid/override", response_model=dict)
async def clear_budget_override(
    req: BudgetBidRequest,
    orchestrator: WorkflowOrchestrator = Depends(get_workflow_orchestrator),
):
    """清除手动设定的日预算，恢复算法推荐"""
    ok = orchestrator.clear_budget_override(req.asin)
    return {"asin": req.asin, "cleared": ok}


# ── P3 统一推荐（LLM 驱动）──────────────────────────────


@router.post("/execution/recommend", response_model=UnifiedRecommendResponse)
async def unified_recommendation(
    req: UnifiedRecommendRequest,
    orchestrator: WorkflowOrchestrator = Depends(get_workflow_orchestrator),
):
    """P3 统一推荐：LLM 同时给出目标 ACOS 和预算/Bid 建议

    - 首次调用触发 LLM 分析，结果缓存至次日 5:00
    - refresh=true 强制跳过缓存重新推荐
    - LLM 失败时降级为算法推荐
    """
    return await orchestrator.get_unified_recommendation(req.asin, refresh=req.refresh, days=req.days)

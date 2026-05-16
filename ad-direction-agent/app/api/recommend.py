"""POST /recommend — 获取默认推荐"""

from fastapi import APIRouter, Depends
from app.models.tag import RecommendRequest
from app.models.decision import RecommendResponse
from app.api.deps import get_data_aggregator, get_recommender
from app.core.data_aggregator import DataAggregator
from app.core.recommender import Recommender

router = APIRouter()


@router.post("/recommend", response_model=RecommendResponse)
async def recommend(
    req: RecommendRequest,
    aggregator: DataAggregator = Depends(get_data_aggregator),
    recommender: Recommender = Depends(get_recommender),
):
    """获取 ASIN 的广告方向默认推荐"""
    data = await aggregator.fetch(req.asin)
    return recommender.recommend(data)

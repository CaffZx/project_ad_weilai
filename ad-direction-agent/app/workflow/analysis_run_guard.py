"""协作取消检查点：由 Campaign 在关键 await 后调用，被取消时抛 AnalysisRunCancelled。"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Awaitable

logger = logging.getLogger(__name__)


class AnalysisRunCancelled(RuntimeError):
    def __init__(self, asin: str, run_id: str):
        super().__init__(f"analysis run cancelled: asin={asin} run_id={run_id}")
        self.asin = asin
        self.run_id = run_id


async def ensure_analysis_run_active(state: Any, asin: str, run_id: str) -> None:
    """读 State 确认本 run 未被取消；已取消则抛 AnalysisRunCancelled。"""
    active = await asyncio.to_thread(state.is_analysis_run_active, asin, run_id)
    if not active:
        raise AnalysisRunCancelled(asin, run_id)

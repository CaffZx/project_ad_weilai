"""异步广告调整 taskId 轮询调度器 — 进程内有界队列 + 固定协程消费者。

设计（spec 2026-08-03-pending-taskid-polling-design §5.2，2026-08-03 修正）：
- 每 Web 进程独立：固定数量 asyncio 协程消费者（ADVERT_TASK_POLL_WORKERS，默认 2）
  从有界队列（ADVERT_TASK_POLL_QUEUE_CAPACITY，默认 20）取任务。
- 单进程已受理上限 = workers + capacity = 22；多进程部署时全局上限 = 进程数 × 单进程值。
- 提交阶段先预留名额；容量满 → 拒绝提交，pending 保持 PENDING。
- 消费者对每个 taskId 按 3m/6m/12m/24m 最多查询四次结果 MCP。
- 不持久化调度状态；进程重启即丢失内存队列，已落库的 pending.task_id 供人工核对。

注意：这里的 "worker" 指 asyncio 协程消费者，不是 Uvicorn/FastAPI 进程 worker。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from app.config.settings import settings

logger = logging.getLogger(__name__)

# 轮询间隔（分钟）：提交后 +3m、随后 +6m、随后 +12m、随后 +24m
_POLL_INTERVALS_MIN = (3, 6, 12, 24)

# 轮询耗尽文案（与 MCP 明确失败区分）
POLL_EXHAUSTED_MSG = (
    "异步任务在 4 次轮询（提交后 +3m、随后 +6m、随后 +12m、随后 +24m）内未返回成功终态；"
    "需人工复核 taskId={task_id}"
)


@dataclass(slots=True)
class PollTask:
    """一个 taskId 的轮询任务：绑定本次精确 pending 行（ops 带 record_kind/pending_id）。"""
    task_id: str
    ops: list[dict]                       # ExecPlan.ops 中 async 相关行（含 record_kind/pending_id/campaign_id）
    poll_fn: Callable[..., Awaitable[dict]]  # 单次结果查询：入参 task_id，返回 {status_by_campaign, message_by_campaign} 或抛异常
    write_fn: Callable[..., Awaitable[None]]  # 终态精确回写：入参 (ops, task_id, campaign_statuses, message_by_campaign, exhausted)
    released: bool = False
    _created_at: float = field(default_factory=time.monotonic)


class TaskPollScheduler:
    """进程内 taskId 轮询调度器。单例由模块级 get_scheduler() 提供。"""

    def __init__(self, workers: int | None = None, capacity: int | None = None):
        self._workers = workers if workers is not None else settings.advert_task_poll_workers
        self._capacity = capacity if capacity is not None else settings.advert_task_poll_queue_capacity
        self._queue: asyncio.Queue[PollTask] = asyncio.Queue(maxsize=self._capacity)
        self._active: set[str] = set()        # 正在轮询的 task_id
        self._reserved: int = 0               # 已预留未入队的名额
        self._pollers: list[asyncio.Task] = []
        self._started = False

    # ── 生命周期 ──

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        loop = asyncio.get_event_loop()
        self._pollers = [
            loop.create_task(self._poller(i), name=f"advert-poll-coro-{i}")
            for i in range(max(self._workers, 1))
        ]
        logger.info("TaskPollScheduler 启动: pollers=%d capacity=%d", self._workers, self._capacity)

    async def shutdown(self) -> None:
        for t in self._pollers:
            t.cancel()
        if self._pollers:
            await asyncio.gather(*self._pollers, return_exceptions=True)
        self._pollers = []
        self._started = False
        self._active.clear()
        self._reserved = 0

    # ── 容量 ──

    @property
    def capacity_available(self) -> bool:
        """单进程已受理上限 = 协程消费者正在轮询 + 队列待轮询 + 已预留名额。"""
        return (len(self._active) + self._queue.qsize() + self._reserved) < (
            self._workers + self._capacity
        )

    @property
    def total_accepted(self) -> int:
        return len(self._active) + self._queue.qsize() + self._reserved

    def reserve(self) -> bool:
        """提交前预留一个已受理名额；容量满返回 False（调用方不得提交 MCP）。"""
        if not self.capacity_available:
            return False
        self._reserved += 1
        return True

    def release(self) -> None:
        """未拿 taskId / 入队失败时释放预留名额。"""
        if self._reserved > 0:
            self._reserved -= 1

    # ── 提交 ──

    def enqueue(self, task: PollTask) -> bool:
        """taskId 落库成功后入队（预留名额转为真实占用）。

        返回 False 表示队列已满——调用方必须视为错误处理：taskId 已落库但无轮询器，
        需告警并转人工核对，不得静默忽略。
        """
        try:
            self._queue.put_nowait(task)
            if self._reserved > 0:
                self._reserved -= 1
            return True
        except asyncio.QueueFull:
            logger.error("轮询队列已满，taskId=%s 无法入队", task.task_id)
            return False

    # ── 协程消费者 ──

    async def _poller(self, index: int) -> None:
        while True:
            task: PollTask = await self._queue.get()
            self._active.add(task.task_id)
            try:
                await self._poll_loop(task)
            except Exception as e:  # noqa: BLE001
                logger.exception("轮询 协程消费者 %d 异常 [taskId=%s]: %s", index, task.task_id, e)
            finally:
                self._active.discard(task.task_id)
                self._queue.task_done()

    async def _poll_loop(self, task: PollTask) -> None:
        """按 3/6/12/24 分钟最多查询四次；每次只回写已终态行，未终态继续轮询。"""
        all_campaign_ids = {
            str(op.get("campaign_id") or "").strip()
            for op in task.ops
            if str(op.get("campaign_id") or "").strip()
        }

        for idx, minutes in enumerate(_POLL_INTERVALS_MIN):
            if idx > 0:
                await asyncio.sleep(minutes * 60)
            else:
                # 首次查询：等待 3 分钟；若入队时已超期（队列等待计入），立即查询
                waited = time.monotonic() - task._created_at
                remaining = minutes * 60 - waited
                if remaining > 0:
                    await asyncio.sleep(remaining)

            try:
                result = await task.poll_fn(task.task_id)
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "轮询第 %d 次异常 [taskId=%s]: %s（继续等待下次查询）",
                    idx + 1, task.task_id, e,
                )
                continue

            status_by_campaign = result.get("status_by_campaign") or {}
            message_by_campaign = result.get("message_by_campaign") or {}
            if status_by_campaign:
                # 只回写已终态的行；未终态行由 write_pending_terminal 保持 IN_PROGRESS
                await task.write_fn(
                    task.ops, task.task_id, status_by_campaign, message_by_campaign,
                    exhausted=False,
                )

            # 全部关联 campaign 已终态 → 任务完成
            if all_campaign_ids and all(
                str(status_by_campaign.get(cid) or "").upper() in {"SUCCESS", "FAIL"}
                for cid in all_campaign_ids
            ):
                logger.info(
                    "轮询终态完成 [taskId=%s] 第 %d 次: %s",
                    task.task_id, idx + 1, status_by_campaign,
                )
                return

            logger.info(
                "轮询第 %d 次 [taskId=%s] 未全部终态: %s",
                idx + 1, task.task_id, status_by_campaign,
            )

        # 四次后仍有未终态行 → 轮询耗尽，写 FAIL（文案区分于平台明确失败）
        await task.write_fn(
            task.ops, task.task_id, {}, {}, exhausted=True,
        )
        logger.warning("轮询耗尽 [taskId=%s] 已按 FAIL 回写", task.task_id)


_scheduler: TaskPollScheduler | None = None


def get_scheduler() -> TaskPollScheduler:
    """进程内单例；首次使用时自动 start（uvicorn worker 内单协程循环）。"""
    global _scheduler
    if _scheduler is None:
        _scheduler = TaskPollScheduler()
        _scheduler.start()
    return _scheduler

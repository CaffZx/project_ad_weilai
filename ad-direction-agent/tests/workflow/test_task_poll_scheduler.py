"""轮询调度器单测：部分终态持续轮询、reserve/release 容量、轮询耗尽回写。"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from app.workflow.steps.task_poll_scheduler import (
    POLL_EXHAUSTED_MSG,
    PollTask,
    TaskPollScheduler,
)


def _make_task(
    task_id: str = "task-1",
    campaign_ids: list[str] | None = None,
    poll_result: dict | None = None,
) -> PollTask:
    campaign_ids = campaign_ids or ["c1"]
    ops = [
        {"record_kind": "campaign", "pending_id": f"p-{cid}", "campaign_id": cid}
        for cid in campaign_ids
    ]
    poll_fn = AsyncMock(return_value=poll_result or {"status_by_campaign": {}, "message_by_campaign": {}})
    write_fn = AsyncMock()
    return PollTask(task_id=task_id, ops=ops, poll_fn=poll_fn, write_fn=write_fn)


def test_reserve_and_release_capacity():
    sched = TaskPollScheduler(workers=1, capacity=2)
    # 上限 = 1 + 2 = 3
    assert sched.reserve() is True
    assert sched.reserve() is True
    assert sched.reserve() is True
    assert sched.reserve() is False  # 满
    sched.release()
    assert sched.reserve() is True
    sched.release()


def test_enqueue_reduces_reserved_slot():
    sched = TaskPollScheduler(workers=1, capacity=2)
    assert sched.reserve() is True
    task = _make_task()
    assert sched.enqueue(task) is True
    # 预留名额转为队列占用
    assert sched.total_accepted == 1
    assert sched.capacity_available is True
    asyncio.run(sched.shutdown())


def test_enqueue_full_returns_false():
    sched = TaskPollScheduler(workers=0, capacity=2)
    assert sched.enqueue(_make_task("t1")) is True
    assert sched.enqueue(_make_task("t2")) is True
    assert sched.enqueue(_make_task("t3")) is False  # 队列满
    asyncio.run(sched.shutdown())


def _run_poll_loop(sched, task) -> None:
    """运行 _poll_loop，轮询间隔压为 0 秒。"""
    import app.workflow.steps.task_poll_scheduler as mod
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(mod, "_POLL_INTERVALS_MIN", (0, 0, 0, 0))
        asyncio.run(sched._poll_loop(task))


def test_poll_loop_partial_terminal_keeps_polling():
    """部分终态不停止轮询：c1 先 SUCCESS，c2 未终态 → 继续查询直到全部终态。"""
    sched = TaskPollScheduler(workers=0, capacity=10)
    calls = [
        # 第一次：c1 SUCCESS, c2 IN_PROGRESS
        {"status_by_campaign": {"c1": "SUCCESS"}, "message_by_campaign": {}},
        # 第二次：c1 SUCCESS（已回写），c2 SUCCESS
        {"status_by_campaign": {"c1": "SUCCESS", "c2": "SUCCESS"}, "message_by_campaign": {}},
    ]
    task = _make_task(campaign_ids=["c1", "c2"])
    task.poll_fn = AsyncMock(side_effect=calls)
    task._created_at = 0  # 强制首次立即查询

    _run_poll_loop(sched, task)

    assert task.poll_fn.await_count == 2
    # 每次有终态都回写；write_fn 被调用 2 次
    assert task.write_fn.await_count == 2
    asyncio.run(sched.shutdown())


def test_poll_loop_all_terminal_stops():
    """全部 campaign 终态 → 一次查询即结束。"""
    sched = TaskPollScheduler(workers=0, capacity=10)
    task = _make_task(
        campaign_ids=["c1", "c2"],
        poll_result={"status_by_campaign": {"c1": "SUCCESS", "c2": "FAIL"}, "message_by_campaign": {}},
    )
    task._created_at = 0

    _run_poll_loop(sched, task)

    assert task.poll_fn.await_count == 1
    assert task.write_fn.await_count == 1
    asyncio.run(sched.shutdown())


def test_poll_loop_exhausted_writes_fail():
    """四次均未全部终态 → 轮询耗尽回写（exhausted=True，未终态行 FAIL）。"""
    sched = TaskPollScheduler(workers=0, capacity=10)
    task = _make_task(
        campaign_ids=["c1"],
        poll_result={"status_by_campaign": {}, "message_by_campaign": {}},
    )
    task._created_at = 0

    _run_poll_loop(sched, task)

    assert task.poll_fn.await_count == 4
    assert task.write_fn.await_count == 1
    args = task.write_fn.call_args
    assert args.kwargs.get("exhausted") is True
    asyncio.run(sched.shutdown())


def test_poll_exhausted_msg_contains_task_id():
    assert "{task_id}" in POLL_EXHAUSTED_MSG

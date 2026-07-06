"""t_advert_agent_pool_entry sync 逻辑离线单测（KB 21 §7 复评数据源重建）。

全程离线——mock 掉 ErpDualWriterRepository._connect，绝不连真实 ERP 库。
锁住双向同步语义：
  - discovery 入池：live is_strictly_in_low_bid_pool=True 且池表无记录 → INSERT；
  - 手动复评离池：池表有记录但 live 已恢复(超池底) → UPDATE exit_date；
  - live 已删除活动：池表有记录但 live 无该 campaign_id → UPDATE exit_date；
  - campaign_id 空串：全部跳过(UNIQUE KEY 碰撞保护)；
  - 已在池不重复 INSERT(UNIQUE KEY 兜底 + 集合判重)。
"""

from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import patch

from app.models.campaign import CampaignPerf, CampaignUnit
from app.persistence.erp_writer.repository import ErpDualWriterRepository


def _cu(cid, bid=0.20, budget=1.0, child="B0CHILD", perf_cost=None):
    """造一个 CampaignUnit。cid 设为 campaign_id；bid/budget 默认在池底。"""
    perf = CampaignPerf(cost=perf_cost) if perf_cost is not None else CampaignPerf()
    return CampaignUnit(
        campaign_name=f"camp-{cid}",
        campaign_key=f"camp-{cid} x {child}",
        campaign_id=cid,
        child_asin=child,
        keyword_text="kw",
        current_bid=bid,
        current_budget=budget,
        perf_7d=perf,
    )


class _FakeCursor:
    """极简假 cursor,记下 SQL + params,返 fetchall 控制。"""

    def __init__(self):
        self.calls: list[tuple[str, tuple]] = []
        self._fetch_rows: list[dict] = []

    def execute(self, sql, params=()):
        self.calls.append((sql, params))
        return None

    def fetchall(self):
        return list(self._fetch_rows)

    @property
    def rowcount(self):
        return 1

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return None


class _FakeConn:
    def __init__(self, cursor: _FakeCursor):
        self._cur = cursor

    def cursor(self):
        return self._cur

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


def _make_repo(in_pool_rows: list[dict]) -> ErpDualWriterRepository:
    """造一个 ErpDualWriterRepository 假实例,patch _connect 返假连接。

    yield (repo, cur)。
    """
    repo = ErpDualWriterRepository(
        host="fake", port=3306, user="fake", password="fake",
        database="fake", use_tls=False,
    )
    cur = _FakeCursor()
    cur._fetch_rows = in_pool_rows
    conn = _FakeConn(cur)

    @contextmanager
    def _patched():
        with patch.object(repo, "_connect", return_value=conn):
            yield repo, cur

    return _patched()


def _extract(cur: _FakeCursor, sql_substring: str) -> list[tuple]:
    return [c for c in cur.calls if sql_substring in c[0]]


# ── discovery 入池 ───────────────────────────────────────────────

def test_discovery_insert_for_in_pool_live_not_in_table():
    # live 在池但池表无该 campaign_id → INSERT discovery
    live = [_cu("C1", bid=0.10, budget=1.0, perf_cost=5.0)]
    with _make_repo(in_pool_rows=[]) as (repo, cur):
        out = repo.sync_pool_entries("B0PARENT", live, shop_account="acct")
    inserts = _extract(cur, "INSERT INTO t_advert_agent_pool_entry")
    assert len(inserts) == 1
    assert out["inserted"] == 1 and out["exited"] == 0
    _, params = inserts[0]
    assert "discovery" in params
    assert 5.0 in params


def test_no_insert_when_already_in_table():
    live = [_cu("C1", bid=0.10, budget=1.0)]
    with _make_repo(in_pool_rows=[{"campaign_id": "C1"}]) as (repo, cur):
        repo.sync_pool_entries("B0PARENT", live, shop_account="acct")
    assert _extract(cur, "INSERT INTO t_advert_agent_pool_entry") == []


def test_no_insert_when_live_not_in_pool():
    live = [_cu("C2", bid=0.50, budget=10.0)]
    with _make_repo(in_pool_rows=[]) as (repo, cur):
        repo.sync_pool_entries("B0PARENT", live, shop_account="acct")
    assert _extract(cur, "INSERT INTO t_advert_agent_pool_entry") == []


# ── 离池方向 ────────────────────────────────────────────────────

def test_exit_when_live_recovered_above_pool_floor():
    live = [_cu("C1", bid=0.50, budget=10.0)]
    with _make_repo(in_pool_rows=[{"campaign_id": "C1"}]) as (repo, cur):
        out = repo.sync_pool_entries("B0PARENT", live, shop_account="acct")
    updates = _extract(cur, "UPDATE t_advert_agent_pool_entry")
    assert len(updates) == 1
    assert out["exited"] == 1


def test_exit_when_live_missing_campaign():
    live = []
    with _make_repo(in_pool_rows=[{"campaign_id": "C1"}]) as (repo, cur):
        out = repo.sync_pool_entries("B0PARENT", live, shop_account="acct")
    updates = _extract(cur, "UPDATE t_advert_agent_pool_entry")
    assert len(updates) == 1
    assert out["exited"] == 1


def test_no_exit_when_still_in_pool():
    live = [_cu("C1", bid=0.10, budget=1.0)]
    with _make_repo(in_pool_rows=[{"campaign_id": "C1"}]) as (repo, cur):
        repo.sync_pool_entries("B0PARENT", live, shop_account="acct")
    assert _extract(cur, "UPDATE t_advert_agent_pool_entry") == []


# ── campaign_id 空串保护 ────────────────────────────────────────

def test_empty_campaign_id_skipped():
    live = [_cu("", bid=0.10, budget=1.0)]
    with _make_repo(in_pool_rows=[]) as (repo, cur):
        repo.sync_pool_entries("B0PARENT", live, shop_account="acct")
    assert _extract(cur, "INSERT INTO t_advert_agent_pool_entry") == []


# ── get_active_entries ──────────────────────────────────────────

def test_get_active_entries_dedupes_by_campaign_id():
    rows = [
        {"campaign_id": "C1", "entry_date": "2026-07-05", "eliminate_spend_7d": 8.0},
        {"campaign_id": "C1", "entry_date": "2026-06-01", "eliminate_spend_7d": 12.0},
        {"campaign_id": "C2", "entry_date": "2026-07-04", "eliminate_spend_7d": None},
    ]
    repo = ErpDualWriterRepository(
        host="fake", port=3306, user="fake", password="fake",
        database="fake", use_tls=False,
    )
    cur = _FakeCursor()
    conn = _FakeConn(cur)
    with patch.object(repo, "_connect", return_value=conn), patch.object(cur, "fetchall", return_value=rows):
        out = repo.get_active_entries("B0PARENT")
    assert set(out.keys()) == {"C1", "C2"}
    assert out["C1"]["eliminate_spend_7d"] == 8.0
    assert out["C2"]["eliminate_spend_7d"] is None


def test_get_active_entries_skips_empty_campaign_id():
    rows = [{"campaign_id": "", "entry_date": "2026-07-05", "eliminate_spend_7d": 1.0}]
    repo = ErpDualWriterRepository(
        host="fake", port=3306, user="fake", password="fake",
        database="fake", use_tls=False,
    )
    cur = _FakeCursor()
    conn = _FakeConn(cur)
    with patch.object(repo, "_connect", return_value=conn), patch.object(cur, "fetchall", return_value=rows):
        out = repo.get_active_entries("B0PARENT")
    assert out == {}

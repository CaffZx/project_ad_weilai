"""make_date_window 按站点当地时间口径单测。

窗口语义：end = 站点当地今天 - 1（最后一个完整日，排除未完整当天）；
start = 站点当地今天 - days；[today-days, today-1] 含两端共 days 天。
站点时区硬编码：US=洛杉矶、UK=伦敦、DE=柏林；未知站点回落默认站并告警。
"""
from __future__ import annotations

import datetime as dt
from unittest.mock import patch
from zoneinfo import ZoneInfo

from app.data import mcp_mapping as mm


def test_window_excludes_today_span_days():
    with patch.object(mm, "_site_today", return_value=dt.date(2026, 6, 24)):
        start, end = mm.make_date_window(7, "Amazon_US")
    assert end == "2026-06-23"      # 当地今天 - 1（排除未完整当天）
    assert start == "2026-06-17"    # 当地今天 - 7
    assert (dt.date.fromisoformat(end) - dt.date.fromisoformat(start)).days + 1 == 7


def test_window_other_spans():
    with patch.object(mm, "_site_today", return_value=dt.date(2026, 6, 24)):
        for n in (14, 30):
            start, end = mm.make_date_window(n, "Amazon_UK")
            assert end == "2026-06-23"        # end 恒为当地昨天，与 days 无关
            span = (dt.date.fromisoformat(end) - dt.date.fromisoformat(start)).days + 1
            assert span == n, (n, start, end, span)


def test_days_floor_to_1():
    with patch.object(mm, "_site_today", return_value=dt.date(2026, 6, 24)):
        start, end = mm.make_date_window(0, "Amazon_DE")   # 退化保护：至少 1 天
    assert start == "2026-06-23" and end == "2026-06-23"


# 实跑核实的 6 个站点 → 时区（日志：US/UK/DE/IT/ES/FR）
_EXPECTED_SITE_TZ = {
    "Amazon_US": "America/Los_Angeles",
    "Amazon_UK": "Europe/London",
    "Amazon_DE": "Europe/Berlin",
    "Amazon_IT": "Europe/Rome",
    "Amazon_ES": "Europe/Madrid",
    "Amazon_FR": "Europe/Paris",
}


def test_site_tz_map_covers_six_sites():
    assert set(mm._SITE_TZ) == set(_EXPECTED_SITE_TZ)
    for site, tzname in _EXPECTED_SITE_TZ.items():
        assert mm._SITE_TZ[site] == ZoneInfo(tzname), site


def test_site_today_uses_each_site_tz():
    # 每个站点用各自时区算「当地今天」（多站点核心：不再硬编一个时区）
    for site, tzname in _EXPECTED_SITE_TZ.items():
        assert mm._site_today(site) == dt.datetime.now(ZoneInfo(tzname)).date(), site


def test_unknown_site_falls_back_with_warning():
    with patch.object(mm.logger, "warning") as warn:
        d = mm._site_today("Amazon_ZZ")
    assert d == dt.datetime.now(mm._SITE_TZ[mm._DEFAULT_SITE]).date()
    assert warn.called          # 未知站点必须告警，便于发现新站点


def test_empty_site_uses_default():
    with patch.object(mm, "_site_today", return_value=dt.date(2026, 6, 24)) as st:
        mm.make_date_window(7)          # 不传 site_code
    st.assert_called_once_with(mm._DEFAULT_SITE)

"""A 股交易日历（2026-2027，含沪深交易所官方休市安排）。

交易规则：周一至周五开市，遇法定节假日休市。周末调休上班日不改变市场休市。
2028 年起未内置精确假期表，退化为「周一至周五开市」，并在 meta 中给出提示。
"""
from __future__ import annotations

import datetime as _dt
from typing import Iterable

# 沪深交易所休市日期（官方安排；周末本身休市，无需列出周末）
HOLIDAYS_2026 = [
    # 元旦：1月1日（星期四）至1月3日（星期六）休市
    "2026-01-01", "2026-01-02", "2026-01-03",
    # 春节：2月15日（星期日）至2月23日（星期一）休市
    "2026-02-15", "2026-02-16", "2026-02-17", "2026-02-18", "2026-02-19",
    "2026-02-20", "2026-02-21", "2026-02-22", "2026-02-23",
    # 清明节：4月4日（星期六）至4月6日（星期一）休市
    "2026-04-04", "2026-04-05", "2026-04-06",
    # 劳动节：5月1日（星期五）至5月5日（星期二）休市
    "2026-05-01", "2026-05-02", "2026-05-03", "2026-05-04", "2026-05-05",
    # 端午节：6月19日（星期五）至6月21日（星期日）休市
    "2026-06-19", "2026-06-20", "2026-06-21",
    # 中秋节：9月25日（星期五）至9月27日（星期日）休市
    "2026-09-25", "2026-09-26", "2026-09-27",
    # 国庆节：10月1日（星期四）至10月7日（星期三）休市
    "2026-10-01", "2026-10-02", "2026-10-03", "2026-10-04", "2026-10-05",
    "2026-10-06", "2026-10-07",
]

HOLIDAYS_2027 = [
    # 元旦：1月1日（周五）至3日（周日）
    "2027-01-01", "2027-01-02", "2027-01-03",
    # 春节：2月5日（周五，除夕）至13日（周六）
    "2027-02-05", "2027-02-06", "2027-02-07", "2027-02-08", "2027-02-09",
    "2027-02-10", "2027-02-11", "2027-02-12", "2027-02-13",
    # 清明节：4月3日（周六）至5日（周一）
    "2027-04-03", "2027-04-04", "2027-04-05",
    # 劳动节：5月1日（周六）至5日（周三）
    "2027-05-01", "2027-05-02", "2027-05-03", "2027-05-04", "2027-05-05",
    # 端午节：6月9日（周三）
    "2027-06-09",
    # 中秋节：9月15日（周三）
    "2027-09-15",
    # 国庆节：10月1日（周五）至7日（周四）
    "2027-10-01", "2027-10-02", "2027-10-03", "2027-10-04", "2027-10-05",
    "2027-10-06", "2027-10-07",
]

HOLIDAYS = {_dt.date.fromisoformat(x) for x in (HOLIDAYS_2026 + HOLIDAYS_2027)}
_CALENDAR_COVERED_YEARS = (2026, 2027)
WEEKDAY_NAMES = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def is_trading_day(d: _dt.date) -> bool:
    """是否 A 股交易日。周末与法定节假日休市。"""
    if d.weekday() >= 5:
        return False
    return d not in HOLIDAYS


def calendar_warning(d: _dt.date) -> str | None:
    """超出内置精确假期表范围时给出提示。"""
    if d.year not in _CALENDAR_COVERED_YEARS:
        return f"{d.year} 年假期表未内置，交易日按周一至周五估算，请留意法定节假日休市。"
    return None


def next_trading_days(start: _dt.date, count: int) -> list[_dt.date]:
    """返回严格晚于 start 的 count 个交易日。"""
    out: list[_dt.date] = []
    d = start
    # 最多向前探查 3*count+30 天，防止死循环
    for _ in range(3 * count + 60):
        d += _dt.timedelta(days=1)
        if is_trading_day(d):
            out.append(d)
            if len(out) >= count:
                break
    if len(out) < count:
        raise RuntimeError("交易日历计算异常：无法生成足够的交易日")
    return out


def prev_trading_day(d: _dt.date) -> _dt.date:
    cur = d - _dt.timedelta(days=1)
    for _ in range(3 * 30 + 60):
        if is_trading_day(cur):
            return cur
        cur -= _dt.timedelta(days=1)
    raise RuntimeError("交易日历计算异常：找不到上一交易日")


def trading_days_between(start: _dt.date, end: _dt.date) -> list[_dt.date]:
    """闭区间 [start, end] 内的交易日。"""
    out = []
    d = start
    while d <= end:
        if is_trading_day(d):
            out.append(d)
        d += _dt.timedelta(days=1)
    return out


def weekday_name(d: _dt.date) -> str:
    return WEEKDAY_NAMES[d.weekday()]


def count_trading_days(a: _dt.date, b: _dt.date) -> int:
    """b 相对 a 的交易日偏移量（b>a 为正）。"""
    if a == b:
        return 0
    sign = 1 if b > a else -1
    lo, hi = (a, b) if a < b else (b, a)
    n = sum(1 for x in trading_days_between(lo, hi) if x != lo)
    return sign * n

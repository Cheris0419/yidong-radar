"""历史快照自检：用真实历史日 K 复现用户截图中的触发线（离线运行）。

A) 闽东电力 000993 @ 2026-09-18 09:54:36（盘中）—— 用户截图
B) 哈药股份 600664 @ 2026-08-13 18:20:05（收盘后）—— mini program 官方口径截图

运行: python3 app.py --selftest
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import sys

from . import calendar_cn as cal
from . import engine

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def _load(code: str) -> dict:
    with open(os.path.join(FIXTURES, f"{code}.json"), "r", encoding="utf-8") as f:
        return json.load(f)


def _series_until(fx: dict, cutoff: str) -> list[dict]:
    """以指数交易日历为基准拼 series（股票缺失日按前值填充）。"""
    stock = {b["d"]: b["c"] for b in fx["stock"]}
    index = {b["d"]: b["c"] for b in fx["index"]}
    out: list[dict] = []
    last_stock = None
    for d in sorted(index):
        if d > cutoff:
            break
        if d in stock:
            last_stock = stock[d]
        if last_stock is None:
            continue
        out.append({"d": d, "sc": last_stock, "ic": index[d]})
    return out


def _next_dates(start: str, n: int) -> list[str]:
    d = _dt.date(int(start[0:4]), int(start[4:6]), int(start[6:8]))
    return [x.strftime("%Y%m%d") for x in cal.next_trading_days(d, n)]


class Checker:
    def __init__(self):
        self.rows: list[tuple[str, str, str, bool]] = []

    def check(self, name: str, got, want, tol: float = 0.011):
        if isinstance(got, (int, float)) and isinstance(want, (int, float)):
            ok = abs(got - want) <= tol
        else:
            ok = got == want
        self.rows.append((name, str(got), str(want), ok))
        return ok

    def check_list(self, name: str, got: list, want: list, tol: float = 0.011):
        ok = len(got) == len(want) and all(
            abs(a - b) <= tol if isinstance(a, (int, float)) and isinstance(b, (int, float)) else a == b
            for a, b in zip(got, want)
        )
        detail = " ".join(
            f"{g:g}{'' if abs(g - w) <= tol else f'(期望{w:g})'}"
            for g, w in zip(got, want)
            if isinstance(g, (int, float)) and isinstance(w, (int, float))
        )
        self.rows.append((name, detail, "", ok))
        return ok

    def report(self) -> bool:
        passed = sum(1 for r in self.rows if r[3])
        total = len(self.rows)
        print("")
        print(f"历史快照自检：{passed}/{total} 项通过")
        print("-" * 78)
        for name, got, want, ok in self.rows:
            mark = "PASS" if ok else "FAIL"
            if ok:
                print(f"  [{mark}] {name}")
            else:
                print(f"  [{mark}] {name}  got={got} want={want}")
        print("-" * 78)
        if passed != total:
            print("自检失败：计算结果与参考值不一致。")
        else:
            print("自检通过：计算口径与官方 mini program 截图一致（同向线存在已知的 1 日展示差异，见 METHODOLOGY）。")
        return passed == total


def _rows_map(result: dict) -> dict:
    return {r["key"]: r for r in result["rows"]}


def _rule_prices(row: dict) -> dict:
    return {r["key"]: r["price"] for r in row["rules"]}


def test_000993(c: Checker) -> bool:
    fx = _load("000993")
    cutoff = "20260917"
    series = _series_until(fx, cutoff)
    # 盘中 09:54:36 的现价 / 指数实时值（由截图触发价反解，误差 < 0.2 点）
    price, index_price = 19.77, 2491.40
    series.append({"d": "20260918", "sc": price, "ic": index_price})
    prev_close = series[-2]["sc"]
    future = _next_dates("20260918", 9)
    result = engine.compute(
        series=series,
        price=price,
        index_price=index_price,
        prev_close=prev_close,
        show_t0=True,
        future_dates=future,
        board="main",
        market="sz",
    )
    rows = result["rows"]
    c.check("A 行数/行标签", [r["key"] for r in rows], [f"T+{i}" for i in range(10)])
    c.check("A 日期", [r["date"] for r in rows], [
        "2026-09-18", "2026-09-21", "2026-09-22", "2026-09-23", "2026-09-24",
        "2026-09-28", "2026-09-29", "2026-09-30", "2026-10-08", "2026-10-09",
    ])
    c.check_list("A 触发价", [r["trigger_price"] for r in rows],
                 [20.71, 20.71, 20.71, 22.78, 25.19, 27.90, 28.83, 28.83, 28.83, 28.95])
    c.check_list("A 触发涨幅%", [r["trigger_pct"] for r in rows],
                 [15.27, 4.77, 4.77, 15.24, 27.40, 41.14, 45.82, 45.82, 45.82, 46.41], tol=0.02)
    c.check_list("A 10日线", [_rule_prices(r)["10d"] for r in rows],
                 [20.71, 20.71, 20.71, 22.78, 25.19, 27.90, 30.71, 33.93, 36.09, 36.09])
    c.check_list("A 30日线", [_rule_prices(r)["30d"] for r in rows],
                 [28.83] * 9 + [28.95])
    same_rows = [r for r in rows if "same" in _rule_prices(r)]
    c.check("A 同向线出现行", [r["key"] for r in same_rows], ["T+0", "T+1", "T+2", "T+3"])
    c.check_list("A 同向线价格", [_rule_prices(r)["same"] for r in same_rows],
                 [21.71, 21.71, 23.72, 23.72])
    c.check("A 同向异动次数", result["summary"]["same_dir"]["count"], 3)
    c.check("A 10日偏离值%", result["summary"]["dev10"]["value"], 90.96, tol=0.02)
    c.check("A 10日窗口起点", result["summary"]["dev10"]["base_date"], "2026-09-08")
    c.check("A 30日窗口起点", result["summary"]["dev30"]["base_date"], "2026-08-19")
    c.check("A 无历史清零", (result["reset"]["reset10"], result["reset"]["reset30"]), (None, None))
    return all(r[3] for r in c.rows)


def test_600664(c: Checker) -> bool:
    fx = _load("600664")
    series = _series_until(fx, "20260813")
    price = series[-1]["sc"]
    index_price = series[-1]["ic"]
    c.check("B 快照收盘价", price, 8.86)
    c.check("B 快照指数", index_price, 3926.96, tol=0.015)
    future = _next_dates("20260813", 10)
    result = engine.compute(
        series=series,
        price=price,
        index_price=index_price,
        prev_close=None,
        show_t0=False,
        future_dates=future,
        board="main",
        market="sh",
    )
    rows = result["rows"]
    c.check("B 行数/行标签", [r["key"] for r in rows], [f"T+{i}" for i in range(1, 11)])
    c.check("B 日期", [r["date"] for r in rows], [
        "2026-08-14", "2026-08-17", "2026-08-18", "2026-08-19", "2026-08-20",
        "2026-08-21", "2026-08-24", "2026-08-25", "2026-08-26", "2026-08-27",
    ])
    c.check_list("B 触发价", [r["trigger_price"] for r in rows],
                 [9.10, 9.10, 9.10, 9.10, 9.10, 10.05, 11.14, 12.20, 13.44, 14.88])
    c.check_list("B 触发涨幅%", [r["trigger_pct"] for r in rows],
                 [2.67, 2.67, 2.67, 2.67, 2.67, 13.44, 25.76, 37.68, 51.66, 67.90], tol=0.02)
    c.check_list("B 10日线", [_rule_prices(r)["10d"] for r in rows],
                 [13.66] * 6 + [14.96, 16.53, 17.58, 17.72])
    c.check_list("B 30日线", [_rule_prices(r)["30d"] for r in rows],
                 [9.10] * 5 + [10.05, 11.14, 12.20, 13.44, 14.88])
    c.check("B 10日窗口清零起算", result["reset"]["reset10"], "2026-08-07")
    c.check("B 30日窗口未清零", result["reset"]["reset30"], None)
    same = result["summary"]["same_dir"]
    c.check("B 同向异动次数", (same["count"], same["threshold"]), (1, 4))
    c.check("B 最近一次异动区间", (same["last_event"]["start"], same["last_event"]["end"]),
            ("2026-08-10", "2026-08-11"))
    c.check("B 最近一次异动偏离%", same["last_event"]["pct"], 21.06, tol=0.02)
    c.check("B 10日偏离值%", result["summary"]["dev10"]["value"], 29.87, tol=0.03)
    c.check("B 30日偏离值%", result["summary"]["dev30"]["value"], 192.27, tol=0.03)
    return all(r[3] for r in c.rows)


def run_selftest() -> bool:
    c = Checker()
    ok_a = test_000993(c)
    ok_b = test_600664(c)
    ok = c.report() and ok_a and ok_b
    return ok


if __name__ == "__main__":
    sys.exit(0 if run_selftest() else 1)

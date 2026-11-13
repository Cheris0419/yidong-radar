#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
异动雷达 · 独立复算脚本（QA 阶段 1）
=====================================
不读、不依赖 Lead 的 core/ 代码。自己从公开行情接口拉日 K（磁盘缓存），
独立实现「严重异动监管触发线」算法，复算两个历史快照并逐项断言：

  A) 闽东电力 000993 @ 2026-09-18 09:54:36（盘中；深证综指 live 由期望值反解）
  B) 哈药股份 600664 @ 2026-08-13 18:20（收盘后；上证指数 3926.96）

数据源（主 / 备）：
  - 东方财富 push2his 日 K（股票 fqt=1 前复权；指数不复权）
  - 同花顺 d.10jqka.com.cn last.js 日 K（前复权）——用于独立复核

用法：
  python3 qa/validate_engine.py            # 优先 qa/cache/ 缓存，缺失时联网（带重试）
  python3 qa/validate_engine.py --refresh  # 强制重新拉取

退出码：0 = 全部断言通过；1 = 存在失败项。
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Dict, List, Optional, Tuple

# --------------------------------------------------------------------------- #
# 0. 基础工具
# --------------------------------------------------------------------------- #
ROOT = os.path.dirname(os.path.abspath(__file__))       # .../yidong-radar/qa
CACHE = os.path.join(ROOT, "cache")
TOL = 0.02                                              # 允许舍入差异
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
WEEKDAY_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def r2(x: float) -> float:
    """四舍五入到 2 位小数（half-up，贴合行情软件显示口径）。"""
    return float(Decimal(repr(float(x))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def norm(d: str) -> str:
    d = d.strip()
    if len(d) == 8 and d.isdigit():
        return f"{d[0:4]}-{d[4:6]}-{d[6:8]}"
    return d


def _http_get_urllib(url: str, timeout: int) -> str:
    headers = {"User-Agent": UA, "Accept": "*/*",
               "Accept-Language": "zh-CN,zh;q=0.9",
               "Referer": "https://quote.eastmoney.com/", "Connection": "close"}
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _http_get_curl(url: str, timeout: int) -> str:
    import subprocess
    p = subprocess.run(
        ["curl", "-sS", "--max-time", str(timeout), "-A", UA,
         "-H", "Referer: https://quote.eastmoney.com/", url],
        capture_output=True, text=True)
    if p.returncode != 0 or not p.stdout.strip():
        raise RuntimeError(f"curl rc={p.returncode} stderr={p.stderr[:200]}")
    return p.stdout


def http_get(url: str, retries: int = 6, timeout: int = 20) -> str:
    """带重试的 GET。个别网络环境下 urllib TLS 指纹被拒，此时回退到系统 curl。"""
    last_err: Optional[Exception] = None
    for i in range(retries):
        for fn in (_http_get_urllib, _http_get_curl):
            try:
                return fn(url, timeout)
            except Exception as e:  # noqa: BLE001
                last_err = e
        time.sleep(0.8 * (i + 1))
    raise RuntimeError(f"请求失败（已重试 {retries} 次）：{url} :: {last_err}")


def read_cache(name: str) -> Optional[str]:
    p = os.path.join(CACHE, name)
    if os.path.exists(p):
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    return None


def write_cache(name: str, text: str) -> None:
    os.makedirs(CACHE, exist_ok=True)
    with open(os.path.join(CACHE, name), "w", encoding="utf-8") as f:
        f.write(text)


# --------------------------------------------------------------------------- #
# 1. 数据层
# --------------------------------------------------------------------------- #
EM_KLINE = ("https://push2his.eastmoney.com/api/qt/stock/kline/get"
            "?secid={secid}&fields1=f1,f2,f3,f4,f5,f6"
            "&fields2=f51,f52,f53,f54,f55,f56,f57&klt=101&fqt={fqt}&beg={beg}&end={end}")
THS_KLINE = "http://d.10jqka.com.cn/v6/line/{sym}/01/last.js"


@dataclass
class Bar:
    date: str
    close: float
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0


def _parse_em(raw: str) -> List[Bar]:
    obj = json.loads(raw)
    data = obj.get("data")
    if not data or not data.get("klines"):
        raise RuntimeError("东财返回空数据")
    bars = []
    for line in data["klines"]:
        p = line.split(",")          # 日期,开,收,高,低,量,额
        bars.append(Bar(norm(p[0]), float(p[2]), float(p[1]), float(p[3]), float(p[4])))
    return bars


def _parse_ths(raw: str) -> List[Bar]:
    m = re.search(r"\((\{.*\})\)\s*;?\s*$", raw, re.S)
    if not m:
        raise RuntimeError("同花顺 JSONP 解析失败")
    obj = json.loads(m.group(1))
    bars = []
    for rec in obj["data"].split(";"):
        p = rec.split(",")           # 日期,开,高,低,收,量,额,...
        if len(p) < 6 or not p[1] or not p[4]:
            continue
        bars.append(Bar(norm(p[0]), float(p[4]), float(p[1]), float(p[2]), float(p[3])))
    return bars


def load_em(secid: str, fqt: int, beg: str = "20260401", end: str = "20261112",
            refresh: bool = False) -> List[Bar]:
    name = f"em_{secid.replace('.', '_')}_fqt{fqt}.json"
    raw = None if refresh else read_cache(name)
    if raw is None:
        raw = http_get(EM_KLINE.format(secid=secid, fqt=fqt, beg=beg, end=end))
        _parse_em(raw)
        write_cache(name, raw)
    return _parse_em(raw)


def load_ths(sym: str, refresh: bool = False) -> List[Bar]:
    name = f"ths_{sym}.js"
    raw = None if refresh else read_cache(name)
    if raw is None:
        raw = http_get(THS_KLINE.format(sym=sym))
        _parse_ths(raw)
        write_cache(name, raw)
    return _parse_ths(raw)


# --------------------------------------------------------------------------- #
# 2. 算法引擎（独立实现）
# --------------------------------------------------------------------------- #
@dataclass
class RuleLine:
    key: str
    label: str
    raw_price: float
    price: float
    pct: float
    binding: bool = False


@dataclass
class Row:
    key: str
    date: str
    weekday: str
    is_today: bool
    rules: List[RuleLine] = field(default_factory=list)
    trigger_price_raw: float = 0.0
    trigger_price: float = 0.0
    trigger_pct: float = 0.0
    trigger_rule: str = ""


@dataclass
class ReplayResult:
    reset10: Optional[str] = None
    reset30: Optional[str] = None
    triggers: List[dict] = field(default_factory=list)

    @property
    def last_trigger(self) -> Optional[dict]:
        return self.triggers[-1] if self.triggers else None


class Engine:
    """严重异动触发线引擎（QA 独立复算版）。

    口径：
      - 累计偏离值 = 股票区间涨跌幅 - 同期指数区间涨跌幅
      - 区间涨跌幅 = 窗口末日收盘 / 窗口内最低收盘 - 1（指数取最低收盘所在日 -> 末日）
      - 10 日窗口 = 11 条有效日 K；30 日窗口 = 31 条
      - 触发价 = base_close * (阈值 + I_now / I_base)，阈值 1.00 / 2.00
      - 严重异动触发后按监管期（10 个交易日）清零：沪市只清 10 日线，深市两条都清
      - 未来交易日：股票按现价假设，指数按现值假设
    """

    WINDOWS = {"10d": 11, "30d": 31}
    THRESH = {"10d": 100.0, "30d": 200.0}

    def __init__(self, stock_bars: List[Bar], index_bars: List[Bar],
                 market: str, board: str):
        self.market = market
        self.board = board
        self.bars = {b.date: b.close for b in stock_bars if b.close > 0}
        self.bar_dates = sorted(self.bars)
        self.ic = {b.date: b.close for b in index_bars}
        self.cal = sorted(self.ic)                       # 交易日历
        self.i_now: float = 0.0

    # ---------- 2.1 交易日辅助 ----------
    def next_trading_days(self, d: str, n: int) -> List[str]:
        after = [x for x in self.cal if x > d]
        if len(after) < n:
            raise RuntimeError(f"交易日历不足：{d} 后需要 {n} 天，仅 {len(after)} 天")
        return after[:n]

    @staticmethod
    def weekday(d: str) -> str:
        return WEEKDAY_CN[datetime.strptime(d, "%Y-%m-%d").weekday()]

    # ---------- 2.2 历史重演：严重异动清零 ----------
    def replay(self, snapshot_date: str, current_price: float,
               i_now: float) -> ReplayResult:
        self.i_now = i_now
        hist = [d for d in self.bar_dates if d <= snapshot_date]
        closes = {d: self.bars[d] for d in hist}
        closes[snapshot_date] = current_price                # 快照当日按现价
        pos = {d: i for i, d in enumerate(hist)}
        n = len(hist)
        resets = {"10d": None, "30d": None}
        triggers: List[dict] = []

        def first_idx_at_or_after(d: str) -> int:
            for i, x in enumerate(hist):
                if x >= d:
                    return i
            return n - 1

        for t in range(n):
            d = hist[t]
            for kind, N in self.WINDOWS.items():
                rst = resets[kind]
                if rst is None:
                    if t < N - 1:
                        continue
                    start = t - N + 1
                else:
                    start = t - N + 1
                    if rst <= d:
                        start = max(start, first_idx_at_or_after(rst))
                    if start < 0:
                        start = 0
                window = hist[start:t + 1]
                if not window:
                    continue
                base_d = min(window, key=lambda x: (closes[x], x))
                i_base = self.ic.get(base_d)
                i_end = i_now if d == snapshot_date else self.ic.get(d)
                if i_base is None or i_end is None:
                    continue
                dev = (closes[d] / closes[base_d] - 1.0 - (i_end / i_base - 1.0)) * 100.0
                if dev >= self.THRESH[kind] - 1e-9:
                    triggers.append({"date": d, "kind": kind, "value": dev})
                    ci = self.cal.index(d)
                    reset_date = self.cal[ci + 11] if ci + 11 < len(self.cal) else None
                    if reset_date:
                        if self.market == "sh":
                            if resets[kind] is None or reset_date > resets[kind]:
                                resets[kind] = reset_date
                        else:
                            for kk in resets:
                                if resets[kk] is None or reset_date > resets[kk]:
                                    resets[kk] = reset_date
        return ReplayResult(reset10=resets["10d"], reset30=resets["30d"], triggers=triggers)

    # ---------- 2.3 含未来假设 K 的序列 ----------
    def _series(self, snapshot_date: str, current_price: float,
                last_row_date: str) -> Tuple[Dict[str, float], Dict[str, float], List[str]]:
        s: Dict[str, float] = {}
        idx: Dict[str, float] = {}
        for d in self.bar_dates:
            if d < snapshot_date:
                s[d] = self.bars[d]
        s[snapshot_date] = current_price
        for d in self.cal:
            if d < snapshot_date:
                idx[d] = self.ic[d]
        idx[snapshot_date] = self.i_now
        for d in self.cal:
            if d <= snapshot_date:
                continue
            if d > last_row_date:
                break
            s[d] = current_price
            idx[d] = self.i_now
        dates = sorted(set(s) & set(idx))
        return s, idx, dates

    # ---------- 2.4 窗口 base ----------
    def _base_in_window(self, prices: Dict[str, float], dates: List[str],
                        end_idx: int, N: int,
                        reset_start: Optional[str]) -> Tuple[str, float]:
        start = max(0, end_idx - N + 1)
        if reset_start is not None and reset_start <= dates[end_idx]:
            while start <= end_idx and dates[start] < reset_start:
                start += 1
        window = dates[start:end_idx + 1]
        if not window:
            raise RuntimeError("窗口为空")
        base_d = min(window, key=lambda x: (prices[x], x))    # 最低收盘；并列取最早
        return base_d, prices[base_d]

    # ---------- 2.5 构建快照 ----------
    def build(self, snap: dict, i_now: float,
              replay: Optional[ReplayResult] = None) -> dict:
        self.i_now = i_now
        D, P = snap["date"], snap["price"]
        rows_count = 10
        if snap["show_t0"]:
            row_dates = [D] + self.next_trading_days(D, rows_count - 1)
        else:
            row_dates = self.next_trading_days(D, rows_count)
        if replay is None:
            replay = self.replay(D, P, i_now)
        reset10, reset30 = replay.reset10, replay.reset30
        s, idx, dates = self._series(D, P, row_dates[-1])
        dpos = {d: i for i, d in enumerate(dates)}

        def rule_raw(kind: str, row_date: str) -> Tuple[float, str, float, float]:
            N = self.WINDOWS[kind]
            rst = reset10 if kind == "10d" else reset30
            base_d, base_c = self._base_in_window(s, dates, dpos[row_date], N, rst)
            i_base = idx[base_d]
            thr = self.THRESH[kind] / 100.0
            raw = base_c * (thr + i_now / i_base)
            return raw, base_d, base_c, i_base

        rows: List[Row] = []
        for k, rd in enumerate(row_dates):
            key = f"T+{k}" if snap["show_t0"] else f"T+{k+1}"
            ref = snap["prev_close"] if snap["show_t0"] and k == 0 else P
            row = Row(key=key, date=rd, weekday=self.weekday(rd), is_today=(rd == D))
            raws: Dict[str, float] = {}
            for kind, label in (("10d", "10日 100%"), ("30d", "30日 200%")):
                raw, _, _, _ = rule_raw(kind, rd)
                raws[kind] = raw
                row.rules.append(RuleLine(kind, label, raw, r2(raw),
                                          r2((raw / ref - 1) * 100)))
            same = self.same_dir_line(snap, key, rd, s, idx, dates,
                                      variant="reference")
            if same is not None:
                row.rules.append(RuleLine("same", "十日同向异动线", same[0], r2(same[0]),
                                          r2((same[0] / ref - 1) * 100)))
            bind = min(row.rules[:2], key=lambda r: r.raw_price)   # same 不参与 binding
            bind.binding = True
            row.trigger_price_raw = bind.raw_price
            row.trigger_price = bind.price
            row.trigger_pct = r2((bind.raw_price / ref - 1) * 100)
            row.trigger_rule = bind.key
            rows.append(row)

        base = {}
        for kind in ("10d", "30d"):
            raw, base_d, base_c, i_base = rule_raw(kind, D)
            base[kind] = {"trigger_price": r2(raw), "base_date": base_d,
                          "base_close": base_c, "i_base": i_base}
        return {"row_dates": row_dates, "rows": rows, "replay": replay,
                "reset": {"10d": reset10, "30d": reset30,
                          "last_trigger": replay.last_trigger},
                "base": base, "prev_close": self.prev_close(D)}

    def prev_close(self, D: str) -> Optional[float]:
        prior = [d for d in self.bar_dates if d < D]
        return self.bars[prior[-1]] if prior else None

    # ---------- 2.6 十日同向异动线 ----------
    def same_dir_line(self, snap: dict, row_key: str, row_date: str,
                      s: Dict[str, float], idx: Dict[str, float],
                      dates: List[str], variant: str = "reference"):
        """同向异动线 = base * (0.20 + I_now/I_base)，base = 3 条 K 窗口最低收盘。

        参考截图显示：T+0 行用「截至当日」窗口；T+k(k>=1) 行用「截至前一交易日」
        的窗口（一日滞后）；base 落在未来假设 K 上时该规则不显示。
        """
        D = snap["date"]
        if variant == "reference" and row_key != "T+0":
            prior = [x for x in dates if x <= row_date]
            if len(prior) < 2:
                return None
            win_end = prior[-2]
        else:
            win_end = row_date
        e = dates.index(win_end)
        window = dates[max(0, e - 2):e + 1]
        if len(window) < 3:
            return None
        base_d = min(window, key=lambda x: (s[x], x))
        if variant == "reference" and base_d > D:
            return None
        i_base = idx[base_d]
        raw = s[base_d] * (0.20 + self.i_now / i_base)
        return (raw, base_d, s[base_d], i_base)

    # ---------- 2.7 最近 10 日同向异动次数 ----------
    def same_dir_count(self, snap: dict, i_now: Optional[float] = None) -> dict:
        if i_now is not None:
            self.i_now = i_now
        D, P = snap["date"], snap["price"]
        closes = {d: self.bars[d] for d in self.bar_dates if d <= D}
        closes[D] = P
        last10 = [x for x in self.cal if x <= D][-10:]
        events: List[dict] = []
        skip_until: Optional[str] = None
        for d in last10:
            if d not in closes:
                continue
            if skip_until is not None and d <= skip_until:
                continue
            prior = [x for x in sorted(closes) if x <= d]
            if len(prior) < 3:
                continue
            window = prior[-3:]
            base_d = min(window, key=lambda x: (closes[x], x))
            i_base = self.ic.get(base_d)
            i_end = self.i_now if d == D else self.ic.get(d)
            if i_base is None or i_end is None:
                continue
            dev = (closes[d] / closes[base_d] - 1.0 - (i_end / i_base - 1.0)) * 100.0
            if dev >= 20.0 - 1e-9:
                nxt = [x for x in self.cal if x > base_d]
                start_date = nxt[0] if nxt else d
                events.append({"start": start_date, "end": d, "pct": r2(dev),
                               "base_date": base_d})
                after = [x for x in self.cal if x > d]
                skip_until = after[0] if after else None
        return {"count": len(events),
                "threshold": 3 if self.board in ("gem", "star") else 4,
                "last_event": events[-1] if events else None,
                "events": events}


# --------------------------------------------------------------------------- #
# 3. 参考快照（期望值来自用户真实截图 docs/reference.jpg 及任务说明）
# --------------------------------------------------------------------------- #
SNAPSHOT_A = {
    "label": "A 闽东电力 000993 @ 2026-09-18 09:54:36（盘中）",
    "code": "000993", "name": "闽东电力", "market": "sz", "board": "main",
    "date": "2026-09-18", "time": "09:54:36",
    "price": 19.77, "prev_close": 17.97,
    "index_code": "399106", "index_name": "深证综指",
    "show_t0": True,
    "index_now_hint": (2491.3, 2491.6),
    "em_stock": ("0.000993", 1), "em_index": "0.399106",
    "ths_stock": "hs_000993", "ths_index": "zs_399106",
    "expected": {
        "dates": ["2026-09-18", "2026-09-21", "2026-09-22", "2026-09-23",
                  "2026-09-24", "2026-09-28", "2026-09-29", "2026-09-30",
                  "2026-10-08", "2026-10-09"],
        "trigger_pct": [15.27, 4.77, 4.77, 15.24, 27.40, 41.14, 45.82, 45.82, 45.82, 46.41],
        "trigger_price": [20.71, 20.71, 20.71, 22.78, 25.19, 27.90, 28.83, 28.83, 28.83, 28.95],
        "line10": [20.71, 20.71, 20.71, 22.78, 25.19, 27.90, 30.71, 33.93, 36.09, 36.09],
        "line30": [28.83] * 9 + [28.95],
        "same": {0: 21.71, 1: 21.71, 2: 21.71, 3: 23.72},   # T+4 起不显示
        "base10": ("2026-09-08", 10.43),
        "base30": ("2026-08-19", 9.63),
        "prev_close": 17.97,
    },
}

SNAPSHOT_B = {
    "label": "B 哈药股份 600664 @ 2026-08-13 18:20（收盘后）",
    "code": "600664", "name": "哈药股份", "market": "sh", "board": "main",
    "date": "2026-08-13", "time": "18:20:05",
    "price": 8.86, "prev_close": 8.81,
    "index_code": "000001", "index_name": "上证指数",
    "show_t0": False,
    "index_now": 3926.96,
    "em_stock": ("1.600664", 1), "em_index": "1.000001",
    "ths_stock": "hs_600664", "ths_index": "zs_1A0001",
    "expected": {
        "dates": ["2026-08-14", "2026-08-17", "2026-08-18", "2026-08-19",
                  "2026-08-20", "2026-08-21", "2026-08-24", "2026-08-25",
                  "2026-08-26", "2026-08-27"],
        "trigger_pct": [2.67, 2.67, 2.67, 2.67, 2.67, 13.44, 25.76, 37.68, 51.66, 67.90],
        "trigger_price": [9.10, 9.10, 9.10, 9.10, 9.10, 10.05, 11.14, 12.20, 13.44, 14.88],
        "line10": [13.66] * 6 + [14.96, 16.53, 17.58, 17.72],
        "line30": [9.10] * 5 + [10.05, 11.14, 12.20, 13.44, 14.88],
        "reset10": "2026-08-07",
        "last_trigger": {"kind": "10d", "date": "2026-07-23", "value": 100.04},
        "same_count": 1,
        "same_threshold": 4,
        "last_event": {"start": "2026-08-10", "end": "2026-08-11", "pct": 21.06},
        "prev_close": 8.81,
    },
}


# --------------------------------------------------------------------------- #
# 4. 断言工具
# --------------------------------------------------------------------------- #
class Checker:
    def __init__(self) -> None:
        self.pass_n = 0
        self.fail_n = 0
        self.rows: List[tuple] = []

    def check(self, name: str, expected, actual, tol: float = 0.0) -> bool:
        diff = None
        if actual is None or expected is None:
            ok = actual is expected
        elif isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
            diff = float(actual) - float(expected)
            ok = abs(diff) <= tol + 1e-12
        else:
            ok = expected == actual
        if ok:
            self.pass_n += 1
        else:
            self.fail_n += 1
        self.rows.append((name, expected, actual, diff, ok))
        return ok

    @staticmethod
    def _fmt(v) -> str:
        if v is None:
            return "—"
        if isinstance(v, float):
            return f"{v:.2f}"
        return str(v)

    def table(self, title: str) -> None:
        print(f"\n{title}")
        print("-" * 96)
        print(f"{'检查项':<36}{'期望':>16}{'复算':>16}{'差异':>12}{'结论':>8}")
        print("-" * 96)
        for name, exp, act, diff, ok in self.rows:
            dd = "—" if diff is None else f"{diff:+.4f}"
            print(f"{name:<36}{self._fmt(exp):>16}{self._fmt(act):>16}{dd:>12}"
                  f"{'PASS' if ok else 'FAIL':>8}")
        print("-" * 96)
        print(f"小计：PASS {self.pass_n} / FAIL {self.fail_n} / 共 {self.pass_n + self.fail_n}")


# --------------------------------------------------------------------------- #
# 5. 复算 + 断言
# --------------------------------------------------------------------------- #
def engine_from_em(snap: dict, refresh: bool) -> Engine:
    secid, fqt = snap["em_stock"]
    stock = load_em(secid, fqt, refresh=refresh)
    index = load_em(snap["em_index"], 0, refresh=refresh)
    return Engine(stock, index, snap["market"], snap["board"])


def engine_from_ths(snap: dict, refresh: bool) -> Engine:
    return Engine(load_ths(snap["ths_stock"], refresh=refresh),
                  load_ths(snap["ths_index"], refresh=refresh),
                  snap["market"], snap["board"])


def display_items(eng: Engine, snap: dict, i_now: float,
                  replay: ReplayResult) -> Dict[str, list]:
    """返回用于反解的显示项（触发涨幅/触发价/10日线/30日线/同向异动线）。"""
    res = eng.build(snap, i_now, replay=replay)
    out = {"trigger_pct": [], "trigger_price": [], "line10": [], "line30": [], "same": []}
    for row in res["rows"]:
        rr = {r.key: r for r in row.rules}
        out["trigger_pct"].append(row.trigger_pct)
        out["trigger_price"].append(row.trigger_price)
        out["line10"].append(rr["10d"].price)
        out["line30"].append(rr["30d"].price)
        out["same"].append(rr["same"].price if "same" in rr else None)
    return out


def solve_index_now(eng: Engine, snap: dict) -> Tuple[float, Tuple[float, float],
                                                    float, float]:
    """从 A 的期望值反解盘中指数现值 I_now：网格搜索 + 可行区间。"""
    exp = snap["expected"]
    D, P = snap["date"], snap["price"]
    replay = eng.replay(D, P, 2491.4)     # 历史重演与 I_now 无关（当日无触发）
    allowed = {"trigger_pct": exp["trigger_pct"], "trigger_price": exp["trigger_price"],
               "line10": exp["line10"], "line30": exp["line30"]}
    feasible: List[float] = []
    best_miss, best_i, best_err = 10 ** 9, 2491.4, 1e9
    i = 2490.000
    while i <= 2493.000 + 1e-9:
        items = display_items(eng, snap, i, replay)
        miss, err = 0, 0.0
        for key, exps in allowed.items():
            for got, want in zip(items[key], exps):
                d = abs(got - want)
                err = max(err, d)
                if d > TOL:
                    miss += 1
        for got, want in zip(items["same"], [exp["same"].get(k) for k in range(10)]):
            if want is None:
                if got is not None:
                    miss += 1
            else:
                d = abs(got - want)
                err = max(err, d)
                if d > TOL:
                    miss += 1
        if miss == 0:
            feasible.append(round(i, 4))
        if (miss, err) < (best_miss, best_err):
            best_miss, best_i, best_err = miss, round(i, 4), err
        i = round(i + 0.001, 4)
    if feasible:
        return (round((feasible[0] + feasible[-1]) / 2, 3),
                (feasible[0], feasible[-1]), best_miss, best_err)
    return best_i, (float("nan"), float("nan")), best_miss, best_err


def validate_snapshot(eng: Engine, snap: dict, ck: Checker, i_now: float) -> dict:
    res = eng.build(snap, i_now)
    exp = snap["expected"]
    tag = "A" if snap["show_t0"] else "B"

    # 行日期 / 周几 / 行数 / T+ 编号
    ck.check(f"[{tag}] 行数", 10, len(res["rows"]))
    ck.check(f"[{tag}] 首行编号", "T+0" if snap["show_t0"] else "T+1",
             res["rows"][0].key)
    ck.check(f"[{tag}] 末行编号", "T+9" if snap["show_t0"] else "T+10",
             res["rows"][-1].key)
    ck.check(f"[{tag}] 首行日期", exp["dates"][0], res["rows"][0].date)
    for k, (row, d) in enumerate(zip(res["rows"], exp["dates"])):
        ck.check(f"[{tag}] {row.key} 日期", d, row.date)
        ck.check(f"[{tag}] {row.key} 周几",
                 WEEKDAY_CN[datetime.strptime(d, "%Y-%m-%d").weekday()], row.weekday)
    # 昨收
    ck.check(f"[{tag}] 昨收", exp["prev_close"], r2(res["prev_close"]), 0.001)

    # 触发涨幅 / 触发价
    for k, row in enumerate(res["rows"]):
        ck.check(f"[{tag}] {row.key} 触发涨幅%", exp["trigger_pct"][k],
                 row.trigger_pct, TOL)
        ck.check(f"[{tag}] {row.key} 触发价", exp["trigger_price"][k],
                 row.trigger_price, TOL)
    # 10 日 / 30 日线
    for k, row in enumerate(res["rows"]):
        rr = {r.key: r for r in row.rules}
        ck.check(f"[{tag}] {row.key} 10日线", exp["line10"][k], rr["10d"].price, TOL)
        ck.check(f"[{tag}] {row.key} 30日线", exp["line30"][k], rr["30d"].price, TOL)
    # binding 与触发价一致性（内部一致性）
    for row in res["rows"]:
        rr = {r.key: r for r in row.rules}
        ck.check(f"[{tag}] {row.key} binding=最低价", True,
                 row.trigger_price == min(rr["10d"].price, rr["30d"].price))
        ck.check(f"[{tag}] {row.key} binding 标记唯一", 1,
                 sum(1 for r in row.rules[:2] if r.binding))
    # 同向异动线（A 有参考值；B 无期望值，仅要求不参与 binding）
    if "same" in exp:
        for k, row in enumerate(res["rows"]):
            rr = {r.key: r for r in row.rules}
            if k in exp["same"]:
                ck.check(f"[{tag}] {row.key} 同向异动线", exp["same"][k],
                         rr["same"].price if "same" in rr else None, TOL)
            else:
                ck.check(f"[{tag}] {row.key} 同向异动线(不显示)", None,
                         rr.get("same").price if "same" in rr else None)
    # base
    if "base10" in exp:
        ck.check(f"[{tag}] 10日窗口 base 日", exp["base10"][0],
                 res["base"]["10d"]["base_date"])
        ck.check(f"[{tag}] 10日窗口 base 收盘", exp["base10"][1],
                 r2(res["base"]["10d"]["base_close"]), 0.001)
    if "base30" in exp:
        ck.check(f"[{tag}] 30日窗口 base 日", exp["base30"][0],
                 res["base"]["30d"]["base_date"])
        ck.check(f"[{tag}] 30日窗口 base 收盘", exp["base30"][1],
                 r2(res["base"]["30d"]["base_close"]), 0.001)
    # 监管清零
    if "reset10" in exp:
        ck.check(f"[{tag}] 10日窗口清零起算日", exp["reset10"], res["reset"]["10d"])
    if "last_trigger" in exp:
        lt = res["reset"]["last_trigger"] or {}
        ck.check(f"[{tag}] 最近严重异动日", exp["last_trigger"]["date"], lt.get("date"))
        ck.check(f"[{tag}] 最近严重异动类型", exp["last_trigger"]["kind"], lt.get("kind"))
        ck.check(f"[{tag}] 最近严重异动偏离值%", exp["last_trigger"]["value"],
                 r2(lt.get("value", 0.0)), TOL)
    # 同向异动次数
    if "same_count" in exp:
        sd = eng.same_dir_count(snap, i_now)
        ck.check(f"[{tag}] 同向异动次数", exp["same_count"], sd["count"])
        ck.check(f"[{tag}] 同向异动红线", exp["same_threshold"], sd["threshold"])
        le = sd["last_event"] or {}
        ck.check(f"[{tag}] 最近同向异动起日", exp["last_event"]["start"], le.get("start"))
        ck.check(f"[{tag}] 最近同向异动止日", exp["last_event"]["end"], le.get("end"))
        ck.check(f"[{tag}] 最近同向异动涨幅%", exp["last_event"]["pct"],
                 le.get("pct"), TOL)
    return res


# --------------------------------------------------------------------------- #
# 6. main
# --------------------------------------------------------------------------- #
def main() -> int:
    refresh = "--refresh" in sys.argv
    os.makedirs(CACHE, exist_ok=True)
    print("=" * 96)
    print("异动雷达 · QA 独立复算  |  数据：东方财富 push2his（主） + 同花顺（备）")
    print("=" * 96)

    # [1] 数据
    print("\n[1/6] 加载日 K（磁盘缓存 qa/cache/，缺失或 --refresh 时联网） ...")
    em: Dict[str, Engine] = {}
    ths: Dict[str, Engine] = {}
    for snap in (SNAPSHOT_A, SNAPSHOT_B):
        em[snap["code"]] = engine_from_em(snap, refresh)
        ths[snap["code"]] = engine_from_ths(snap, refresh)
        e, t = em[snap["code"]], ths[snap["code"]]
        print(f"  {snap['code']} {snap['name']}: 东财 {len(e.bar_dates)} 根 [{e.bar_dates[0]}..{e.bar_dates[-1]}]"
              f" | 同花顺 {len(t.bar_dates)} 根 [{t.bar_dates[0]}..{t.bar_dates[-1]}]"
              f" | {snap['index_name']} 东财 {len(e.cal)} 个交易日")

    # [2] 数据源交叉核对
    print("\n[2/6] 数据源交叉核对（主备两源重叠日期收盘价） ...")
    src = Checker()
    for snap in (SNAPSHOT_A, SNAPSHOT_B):
        e, t = em[snap["code"]], ths[snap["code"]]
        common = sorted(set(e.bars) & set(t.bars))
        bad = [(d, e.bars[d], t.bars[d]) for d in common
               if abs(e.bars[d] - t.bars[d]) > 0.005]
        src.check(f"{snap['code']} 股票两源收盘一致（{len(common)} 个重叠日）", 0, len(bad))
        common_i = sorted(set(e.ic) & set(t.ic))
        bad_i = [(d, e.ic[d], t.ic[d]) for d in common_i if abs(e.ic[d] - t.ic[d]) > 0.02]
        src.check(f"{snap['index_code']} 指数两源收盘一致（{len(common_i)} 个重叠日）",
                  0, len(bad_i))
        if bad:
            print("    股票差异：", bad[:3])
        if bad_i:
            print("    指数差异：", bad_i[:3])
    src.table("数据源交叉核对")

    # [3] A 反解 I_now
    print("\n[3/6] 快照 A：盘中指数点位 I_now 从期望值反解（网格 2490.000~2493.000，步长 0.001） ...")
    eng_a = em["000993"]
    i_now_a, feas, miss, err = solve_index_now(eng_a, SNAPSHOT_A)
    if feas[0] == feas[0]:  # not NaN
        print(f"  可行区间：I_now ∈ [{feas[0]:.3f}, {feas[1]:.3f}]（全部期望项四舍五入后一致）")
        print(f"  取中值 I_now = {i_now_a}")
        hint = SNAPSHOT_A["index_now_hint"]
        overlap = max(0.0, min(feas[1], hint[1]) - max(feas[0], hint[0]))
        print(f"  任务提示区间 [{hint[0]}, {hint[1]}] 与可行区间"
              f"{'有交集（宽度 %.3f）' % overlap if overlap > 0 else '无交集'}")
    else:
        print(f"  !! 无可行 I_now；最优 I={i_now_a} 仍有 {miss} 项超差，最大误差 {err:.4f}")

    # [4] A
    print("\n[4/6] 快照 A 逐项复算（闽东电力 000993 @ 2026-09-18 09:54:36） ...")
    ck_a = Checker()
    res_a = validate_snapshot(eng_a, SNAPSHOT_A, ck_a, i_now_a)
    ck_a.table(f"快照 A 对照表（I_now = {i_now_a}，容差 ±{TOL}）")

    # [5] B
    print("\n[5/6] 快照 B 逐项复算（哈药股份 600664 @ 2026-08-13 18:20） ...")
    ck_b = Checker()
    eng_b = em["600664"]
    res_b = validate_snapshot(eng_b, SNAPSHOT_B, ck_b, SNAPSHOT_B["index_now"])
    ck_b.table(f"快照 B 对照表（上证指数 3926.96，容差 ±{TOL}）")

    # [6] 备源复核
    print("\n[6/6] 备源（同花顺）独立重算 A/B 全部项 ...")
    ck_t = Checker()
    res_a_t = validate_snapshot(ths["000993"], SNAPSHOT_A, ck_t, i_now_a)
    res_b_t = validate_snapshot(ths["600664"], SNAPSHOT_B, ck_t, SNAPSHOT_B["index_now"])
    ck_t.table("备源（同花顺数据）复核")
    cross = Checker()
    for tag, r1, r2_ in (("A", res_a, res_a_t), ("B", res_b, res_b_t)):
        for k in range(10):
            cross.check(f"[{tag}] 行{k} 主备触发价一致", r1["rows"][k].trigger_price,
                        r2_["rows"][k].trigger_price)
            x1 = {r.key: r for r in r1["rows"][k].rules}
            x2 = {r.key: r for r in r2_["rows"][k].rules}
            for kk in ("10d", "30d"):
                cross.check(f"[{tag}] 行{k} 主备{kk}一致", x1[kk].price, x2[kk].price)
    cross.table("主备两源独立结果一致性")

    # 补充：口径边界核查
    print("\n[补充] 口径边界核查 ...")
    # 1) 复权口径：000993 于 2026-07-30 除息 0.18 元
    eng_a_raw = Engine(load_em("0.000993", 0), load_em("0.399106", 0), "sz", "main")
    res_a_raw = eng_a_raw.build(SNAPSHOT_A, i_now_a)
    same_flag = all(abs(x.trigger_price - y.trigger_price) < 1e-9
                    for x, y in zip(res_a["rows"], res_a_raw["rows"]))
    print("  · 复权口径：000993 于 2026-07-30 除息 0.18 元；用不复权数据重算快照 A，"
          f"显示值{'完全一致' if same_flag else '存在差异'}（窗口 base 均晚于除息日）")
    # 2) 同向异动线语义口径 vs 参考口径
    seq = {}
    for k, row in enumerate(res_a["rows"]):
        v = eng_a.same_dir_line(SNAPSHOT_A, row.key, row.date,
                                *eng_a._series(SNAPSHOT_A["date"], SNAPSHOT_A["price"],
                                               res_a["row_dates"][-1]),
                                variant="semantic")
        seq[row.key] = None if v is None else r2(v[0])
    print(f"  · 同向异动线：参考截图 T+0..T+3 = 21.71/21.71/21.71/23.72；"
          f"语义口径（不滞后）= {seq}")
    # 3) 沪市 30 日线不清零
    print(f"  · 快照 B（沪市）：重演得到 reset10={res_b['reset']['10d']}, "
          f"reset30={res_b['reset']['30d']}（沪市只清 10 日线，30 日线继续滚动）")
    # 4) 深市快照 A 的监管清零状态
    print(f"  · 快照 A（深市）：重演得到 reset10={res_a['reset']['10d']}, "
          f"reset30={res_a['reset']['30d']}，触发记录 {len(res_a['replay'].triggers)} 条")
    # 5) A 的同向异动次数（参考截图未给，仅供参考）
    sd_a = eng_a.same_dir_count(SNAPSHOT_A, i_now_a)
    print(f"  · 快照 A 同向异动次数（参考截图未给，仅供参考）：{sd_a['count']}/4，"
          f"事件 {[(e['start'], e['end'], e['pct']) for e in sd_a['events']]}")
    # 6) 未来行触发的假设
    print("  · 未来行假设：股票按现价、指数按现值（按未来指数收盘不变估算）")
    # 7) 历史长度
    print(f"  · 历史长度：重演最早可判定严重异动的交易日 ≈ {eng_a.cal[30]}；"
          "更早的触发无法用公开接口的 140~150 根日 K 回溯")

    # 汇总
    total_pass = (src.pass_n + ck_a.pass_n + ck_b.pass_n + ck_t.pass_n + cross.pass_n)
    total_fail = (src.fail_n + ck_a.fail_n + ck_b.fail_n + ck_t.fail_n + cross.fail_n)
    print("\n" + "=" * 96)
    print(f"总结果：PASS {total_pass} / FAIL {total_fail} / 共 {total_pass + total_fail}")
    print(f"  数据源交叉核对：PASS {src.pass_n} / FAIL {src.fail_n}")
    print(f"  快照 A（东财）：PASS {ck_a.pass_n} / FAIL {ck_a.fail_n}")
    print(f"  快照 B（东财）：PASS {ck_b.pass_n} / FAIL {ck_b.fail_n}")
    print(f"  主备源复核：PASS {ck_t.pass_n} / FAIL {ck_t.fail_n}，一致性：PASS {cross.pass_n} / FAIL {cross.fail_n}")
    print("=" * 96)
    return 0 if total_fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

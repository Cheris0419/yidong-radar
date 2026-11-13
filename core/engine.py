"""严重异动监管触发线计算引擎（纯函数，无网络依赖，便于历史快照自检）。

计算口径（经用户提供的 mini program 真实截图逐项验证）：
  累计偏离值 = 区间最低收盘价起算的股票区间涨跌幅 − 同期指数区间涨跌幅
  10 日窗口 = 最近 11 条有效日 K；30 日窗口 = 最近 31 条有效日 K
  触发价 X = base_close × (阈值 + I_now / I_base)，阈值 1.00 / 2.00
  未来交易日：股票收盘按当前价假设、指数按当前点位假设（“按未来指数收盘不变估算”）
  严重异动触发后：监管期 10 个交易日，清零从监管期结束后一交易日起算
    - 沪市：10 日 100% 触发后仅 10 日线清零，30 日线继续滚动
    - 深市：任一条线触发后两条线均清零
  普通异动：连续 3 条 K（3 个交易日）的区间偏离 >= ±20%（主板）/ ±30%（创业板、科创板）
  同向次数红线：最近 10 个交易日内主板 4 次、创业板/科创板 3 次
"""
from __future__ import annotations

import math
from typing import Optional

# ---- 常量 -------------------------------------------------------------
THRESHOLD_10D = 1.0          # +100%
THRESHOLD_30D = 2.0          # +200%
RESET_MARGIN = 11            # 触发日之后第 11 个交易日开始新窗口（监管期 10 个交易日）

BOARD_CONFIG = {
    # board: (普通异动单日阈值, 严重异动同向次数红线)
    "main": (0.20, 4),
    "gem": (0.30, 3),
    "star": (0.30, 3),
}
DEFAULT_BOARD = (0.20, 4)

LEVEL_RANK = {"safe": 0, "watch": 1, "high": 2}
LEVEL_LABEL = {"safe": "安全", "watch": "关注", "high": "高危"}
RULE_LABEL = {"10d": "10日 100%", "30d": "30日 200%", "same": "十日同向异动线"}


def round_half_up(x: float, ndigits: int = 2) -> float:
    """四舍五入（Python 内置 round 为银行家舍入，这里用于复现行情软件展示值）。"""
    if x is None:
        return 0.0
    factor = 10 ** ndigits
    if x >= 0:
        return math.floor(x * factor + 0.5) / factor
    return -math.floor(-x * factor + 0.5) / factor


def iso(d: str) -> str:
    """'20260918' -> '2026-09-18'"""
    return f"{d[0:4]}-{d[4:6]}-{d[6:8]}"


def week_name(d: str) -> str:
    import datetime as _dt
    dt = _dt.date(int(d[0:4]), int(d[4:6]), int(d[6:8]))
    return "周" + "一二三四五六日"[dt.weekday()]


def _level_from_ratio(ratio: float) -> str:
    if ratio >= 0.8:
        return "high"
    if ratio >= 0.5:
        return "watch"
    return "safe"


def _dev_level_from_value(value: float, threshold: float) -> str:
    if value is None:
        return "safe"
    return _level_from_ratio(value / threshold if threshold else 0.0)


def _max_level(*levels: str) -> str:
    best = "safe"
    for lv in levels:
        if LEVEL_RANK.get(lv, 0) > LEVEL_RANK.get(best, 0):
            best = lv
    return best


# ---- 内部工具 ---------------------------------------------------------

def _window(seq: list[dict], end: int, lookback: int, reset_idx: Optional[int]) -> tuple[int, int]:
    """返回窗口 [start, end] 的起止下标（闭区间）。lookback 为 K 线条数。"""
    start = max(0, end - (lookback - 1))
    if reset_idx is not None and reset_idx <= end:
        start = max(start, reset_idx)
    return start, end


def _argmin_close(seq: list[dict], start: int, end: int) -> int:
    """区间内收盘价最低的 K 线下标；并列取最早。"""
    best = start
    best_v = seq[start]["sc"]
    for i in range(start + 1, end + 1):
        v = seq[i]["sc"]
        if v < best_v:
            best = i
            best_v = v
    return best


def _trigger_price(seq: list[dict], base_idx: int, threshold: float, index_price: float) -> tuple[float, float]:
    """触发价与比率 I_now / I_base。"""
    base_close = seq[base_idx]["sc"]
    base_index = seq[base_idx]["ic"]
    ratio = (index_price / base_index) if base_index else 1.0
    return base_close * (threshold + ratio), ratio


def _deviation(seq: list[dict], base_idx: int, end_idx: int, index_price: float) -> tuple[float, float, float]:
    """返回 (股票区间涨跌幅, 指数区间涨跌幅, 累计偏离值)。"""
    base = seq[base_idx]
    stock = seq[end_idx]["sc"] / base["sc"] - 1.0
    index = index_price / base["ic"] - 1.0
    return stock, index, stock - index


# ---- 严重异动历史重演（窗口清零） -------------------------------------

def simulate_resets(seq: list[dict], market: str) -> dict:
    """从序列起点逐日重演严重异动触发与监管期清零。

    返回 {reset10_idx, reset30_idx, log}
    """
    n = len(seq)
    reset10_idx: Optional[int] = None
    reset30_idx: Optional[int] = None
    pending10: Optional[int] = None   # 待生效下标
    pending30: Optional[int] = None
    log: list[dict] = []

    for i in range(n):
        if pending10 is not None and i >= pending10:
            reset10_idx = i
            pending10 = None
            log.append({"kind": "reset10", "idx": i, "date": seq[i]["d"]})
        if pending30 is not None and i >= pending30:
            reset30_idx = i
            pending30 = None
            log.append({"kind": "reset30", "idx": i, "date": seq[i]["d"]})
        if i < 31:
            continue

        # 10 日
        start, end = _window(seq, i, 11, reset10_idx)
        b = _argmin_close(seq, start, end)
        _, _, dev10 = _deviation(seq, b, i, seq[i]["ic"])
        # 30 日
        start30, end30 = _window(seq, i, 31, reset30_idx)
        b30 = _argmin_close(seq, start30, end30)
        _, _, dev30 = _deviation(seq, b30, i, seq[i]["ic"])

        trig10 = pending10 is None and dev10 >= THRESHOLD_10D - 1e-9
        trig30 = pending30 is None and dev30 >= THRESHOLD_30D - 1e-9
        if trig10:
            pending10 = i + RESET_MARGIN
            log.append({"kind": "10d", "idx": i, "date": seq[i]["d"], "value": round_half_up(dev10 * 100, 2)})
        if trig30:
            pending30 = i + RESET_MARGIN
            log.append({"kind": "30d", "idx": i, "date": seq[i]["d"], "value": round_half_up(dev30 * 100, 2)})
        if market == "sz" and (trig10 or trig30):
            # 深市：触发后两条线均清零
            if pending10 is None:
                pending10 = i + RESET_MARGIN
            if pending30 is None:
                pending30 = i + RESET_MARGIN

    return {"reset10_idx": reset10_idx, "reset30_idx": reset30_idx, "log": log}


# ---- 普通异动（3 条 K 窗口） ------------------------------------------

def scan_events(seq: list[dict], event_threshold: float) -> list[dict]:
    """扫描最近 10 个交易日的上涨同向普通异动（窗口为 3 条 K）。

    命中后跳过下一日（避免重叠窗口重复计数）。
    """
    n = len(seq)
    if n < 12:
        return []
    start = max(2, n - 10)
    events: list[dict] = []
    s = start
    while s <= n - 1:
        b = _argmin_close(seq, max(0, s - 2), s)
        stock, index, dev = _deviation(seq, b, s, seq[s]["ic"])
        if dev >= event_threshold - 1e-9:
            base_date = seq[b]["d"]
            # 展示区间：起算点后的第一个交易日至末日
            start_date = seq[b + 1]["d"] if b + 1 <= s else base_date
            events.append(
                {
                    "base_idx": b,
                    "end_idx": s,
                    "start": iso(start_date),
                    "end": iso(seq[s]["d"]),
                    "pct": round_half_up(dev * 100, 2),
                }
            )
            s += 2
        else:
            s += 1
    return events


# ---- 主入口 -----------------------------------------------------------

def compute(
    series: list[dict],
    price: float,
    index_price: float,
    prev_close: Optional[float],
    show_t0: bool,
    future_dates: list[str],
    board: str = "main",
    market: str = "sz",
    rows_count: int = 10,
) -> dict:
    """计算严重异动触发线。

    参数
    ----
    series: [{'d': 'YYYYMMDD', 'sc': 收盘价, 'ic': 指数点位}]，升序，最后一条为 hist_end
    price: 当前股价（= series[-1]['sc']）
    index_price: 当前指数点位（= series[-1]['ic']）
    prev_close: 昨收（show_t0=True 时必填，用于 T+0 触发涨幅）
    show_t0: 是否展示 T+0 行（交易时段内为 True；收盘后为 False，行从 T+1 开始）
    future_dates: hist_end 之后的交易日 'YYYYMMDD' 列表，长度 = rows_count - (1 if show_t0 else 0)
    board: main / gem / star
    market: sh / sz
    """
    if not series or len(series) < 2:
        raise ValueError("历史行情不足，无法计算触发线")

    event_threshold, redline = BOARD_CONFIG.get(board, DEFAULT_BOARD)
    sim = simulate_resets(series, market)
    reset10_idx: Optional[int] = sim["reset10_idx"]
    reset30_idx: Optional[int] = sim["reset30_idx"]

    seq = series
    hist_end = seq[-1]["d"]

    # ---- 投影序列：历史 + 未来（未来股票按现价、指数按现点位） ----
    dates = [b["d"] for b in seq] + list(future_dates)
    sc = [b["sc"] for b in seq] + [price] * len(future_dates)
    ic = [b["ic"] for b in seq] + [index_price] * len(future_dates)
    proj = [{"d": dates[i], "sc": sc[i], "ic": ic[i]} for i in range(len(dates))]

    # ---- 同向异动计数 ----
    events = scan_events(seq, event_threshold)
    count = len(events)
    same_level = "high" if count >= redline - 1 else ("watch" if count >= 1 else "safe")
    last_event = events[-1] if events else None

    def same_count_at(j: int) -> int:
        """目标日 j 的最近 10 个交易日内已计入的同向异动次数。"""
        edge = j - 9
        return sum(1 for e in events if e["end_idx"] >= edge)

    # ---- 逐行计算 ----
    rows: list[dict] = []
    base_pos = len(seq) - 1
    for k in range(rows_count):
        j = base_pos + k if show_t0 else base_pos + 1 + k
        if j >= len(proj):
            break
        is_today = show_t0 and k == 0
        rule_prices: list[tuple[str, float]] = []

        b10 = _argmin_close(proj, *_window(proj, j, 11, reset10_idx))
        x10, _ = _trigger_price(proj, b10, THRESHOLD_10D, index_price)
        rule_prices.append(("10d", x10))

        b30 = _argmin_close(proj, *_window(proj, j, 31, reset30_idx))
        x30, _ = _trigger_price(proj, b30, THRESHOLD_30D, index_price)
        rule_prices.append(("30d", x30))

        include_same = same_count_at(j) >= redline - 1
        x_same = None
        if include_same:
            b3 = _argmin_close(proj, max(0, j - 2), j)
            x_same, _ = _trigger_price(proj, b3, event_threshold, index_price)
            rule_prices.append(("same", x_same))

        ref_price = prev_close if is_today else price
        if ref_price is None or ref_price <= 0:
            ref_price = price
        binding_key, binding_price = min(rule_prices, key=lambda kv: kv[1])
        trigger_pct = binding_price / ref_price - 1.0

        rules = []
        for key, x in rule_prices:
            rules.append(
                {
                    "key": key,
                    "label": RULE_LABEL[key],
                    "price": round_half_up(x, 2),
                    "pct": round_half_up((x / ref_price - 1.0) * 100, 2),
                    "binding": key == binding_key,
                }
            )

        offset = k if show_t0 else k + 1
        rows.append(
            {
                "key": f"T+{offset}",
                "date": iso(proj[j]["d"]),
                "weekday": week_name(proj[j]["d"]),
                "is_today": is_today,
                "trigger_pct": round_half_up(trigger_pct * 100, 2),
                "trigger_price": round_half_up(binding_price, 2),
                "trigger_rule": binding_key,
                "rules": rules,
            }
        )

    # ---- 当前偏离值卡片 ----
    def summary_card(lookback: int, threshold: float, reset_idx: Optional[int], label: str) -> dict:
        start, end = _window(seq, len(seq) - 1, lookback, reset_idx)
        b = _argmin_close(seq, start, end)
        stock, index, dev = _deviation(seq, b, end, index_price)
        trigger, ratio = _trigger_price(seq, b, threshold, index_price)
        level = _dev_level_from_value(dev, threshold)
        return {
            "value": round_half_up(dev * 100, 2),
            "threshold": round_half_up(threshold * 100, 2),
            "remaining": round_half_up((threshold - dev) * 100, 2),
            "progress": round(dev / threshold, 4) if threshold else 0.0,
            "stock_change": round_half_up(stock * 100, 2),
            "index_change": round_half_up(index * 100, 2),
            "window_start": iso(seq[b]["d"]),
            "window_end": iso(hist_end),
            "base_close": round_half_up(seq[b]["sc"], 2),
            "base_date": iso(seq[b]["d"]),
            "trigger_price": round_half_up(trigger, 2),
            "level": level,
            "label": LEVEL_LABEL[level],
        }

    dev10 = summary_card(11, THRESHOLD_10D, reset10_idx, "10日100%")
    dev30 = summary_card(31, THRESHOLD_30D, reset30_idx, "30日200%")

    same_dir = {
        "count": count,
        "threshold": redline,
        "remaining": max(0, redline - count),
        "progress": round(count / redline, 4) if redline else 0.0,
        "level": same_level,
        "label": LEVEL_LABEL[same_level],
        "rule": f"连续3日累计偏离+{int(event_threshold * 100)}% · 严重异动红线：10日内{redline}次上涨同向",
        "last_event": last_event,
        "events": events,
    }

    # ---- 头部风险徽标 ----
    header_level = _max_level(dev10["level"], dev30["level"], same_dir["level"])
    reasons: list[str] = []
    if dev10["level"] != "safe":
        reasons.append(f"10日偏离值 {dev10['value']:.2f}%（阈值 {dev10['threshold']:.0f}%）")
    if dev30["level"] != "safe":
        reasons.append(f"30日偏离值 {dev30['value']:.2f}%（阈值 {dev30['threshold']:.0f}%）")
    if count > 0:
        reasons.append(f"10日同向异动 {count}/{redline}")

    # ---- 清零信息 ----
    trigger_logs = [x for x in sim["log"] if x["kind"] in ("10d", "30d")]
    last_trigger = None
    if trigger_logs:
        t = trigger_logs[-1]
        last_trigger = {"kind": t["kind"], "date": iso(t["date"]), "value": t.get("value")}
    reset = {
        "reset10": iso(seq[reset10_idx]["d"]) if reset10_idx is not None else None,
        "reset30": iso(seq[reset30_idx]["d"]) if reset30_idx is not None else None,
        "last_trigger": last_trigger,
    }

    return {
        "summary": {"dev10": dev10, "dev30": dev30, "same_dir": same_dir},
        "risk": {"level": header_level, "label": LEVEL_LABEL[header_level], "reasons": reasons},
        "reset": reset,
        "rows": rows,
    }

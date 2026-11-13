#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
异动雷达 · 阶段 2 端到端验收脚本（QA）
=====================================
对 http://127.0.0.1:8765 做黑盒验收（不读后端源码）：
  1. /api/health、/api/search、/api/triggers 字段/类型/结构
  2. 历史快照回放 A/B：与用户参考截图逐项比对
  3. 与 QA 独立引擎（qa/validate_engine.py）在「同一实时行情输入」下逐行比对
     （实时 000993、沪市 600664、创业板 300750 回归）
  4. rows/summary/risk/reset 内部一致性、T+ 编号/日期/周几、binding 与触发价
  5. 错误码、交易阶段（pre_open/trading/lunch/closed/holiday）、show_t0 逻辑
  6. 前端静态资源可达性（零 CDN、控制台报错留给截图脚本）

用法：python3 qa/e2e_checks.py [--base http://127.0.0.1:8765]
退出码：0 = 无 FAIL；1 = 有 FAIL。
"""
from __future__ import annotations

import json
import os
import sys
import urllib.parse
from datetime import datetime

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
import validate_engine as V  # noqa: E402  复用阶段 1 的独立引擎/数据层（非 Lead 代码）

BASE = "http://127.0.0.1:8765"

# ------------------------------ 工具 ------------------------------ #
class Ck:
    def __init__(self) -> None:
        self.pass_n = 0
        self.fail_n = 0
        self.fails: list[tuple] = []

    def check(self, name: str, expected, actual, tol: float = 0.0,
              detail: str = "") -> bool:
        diff = None
        if expected is None or actual is None:
            ok = expected is actual
        elif isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
            diff = float(actual) - float(expected)
            ok = abs(diff) <= tol + 1e-12
        else:
            ok = expected == actual
        if ok:
            self.pass_n += 1
        else:
            self.fail_n += 1
            self.fails.append((name, expected, actual, diff))
        return ok

    def section(self, title: str) -> None:
        print(f"\n{'=' * 90}\n{title}\n{'=' * 90}")

    def summary(self, title: str) -> None:
        print(f"\n--- {title}: PASS {self.pass_n} / FAIL {self.fail_n} ---")
        for item in self.fails:
            name, exp, act, diff = item[:4]
            detail = item[4] if len(item) > 4 else ""
            d = "" if diff is None else f" (diff={diff:+.4f})"
            print(f"  FAIL {name}: 期望={exp!r} 实际={act!r}{d}"
                  + (f" :: {detail}" if detail else ""))


def api_get(path: str, timeout: int = 60) -> tuple[int, dict | str, dict]:
    """返回 (http_code, json_or_text, headers)"""
    import subprocess
    url = BASE + path
    p = subprocess.run(
        ["curl", "-sS", "--max-time", str(timeout), "-D", "-", url],
        capture_output=True, text=True)
    raw = p.stdout.replace("\r\n", "\n")
    if "\n\n" in raw:
        head, body = raw.split("\n\n", 1)
    else:
        head, body = "", raw
    status = 0
    headers: dict[str, str] = {}
    for line in head.splitlines():
        if line.startswith("HTTP/"):
            try:
                status = int(line.split()[1])
            except (IndexError, ValueError):
                pass
        elif ":" in line:
            k, v = line.split(":", 1)
            headers[k.strip().lower()] = v.strip()
    try:
        return status, json.loads(body), headers
    except json.JSONDecodeError:
        return status, body, headers


def approx(a: float, b: float, tol: float = 0.02) -> bool:
    return abs(a - b) <= tol + 1e-9


# ------------------------------ 1. health / search ------------------------------ #
def check_health(ck: Ck) -> None:
    ck.section("[1] /api/health")
    code, body, headers = api_get("/api/health")
    ck.check("HTTP 200", 200, code)
    ck.check("Content-Type JSON", True,
             headers.get("content-type", "").startswith("application/json"))
    ck.check("charset=utf-8", True, "charset=utf-8" in headers.get("content-type", ""))
    d = body.get("data", {}) if isinstance(body, dict) else {}
    ck.check("ok=true", True, body.get("ok") if isinstance(body, dict) else None)
    ck.check("status=ok", "ok", d.get("status"))
    ck.check("version 非空", True, bool(d.get("version")))
    t = d.get("time", "")
    try:
        datetime.fromisoformat(t)
        iso_ok = True
    except (ValueError, TypeError):
        iso_ok = False
    ck.check("time 为 ISO8601", True, iso_ok)
    ck.check("time 带时区", True, "+" in t or t.endswith("Z"))


def check_search(ck: Ck, findings: list[str]) -> None:
    ck.section("[2] /api/search")
    # 代码搜索
    code, body, _ = api_get("/api/search?" + urllib.parse.urlencode({"q": "000993"}))
    ck.check("search 000993 200", 200, code)
    items = body["data"]["items"]
    ck.check("search 000993 命中 1 条", 1, len(items))
    if items:
        it = items[0]
        ck.check("code", "000993", it.get("code"))
        ck.check("name", "闽东电力", it.get("name"))
        ck.check("market", "sz", it.get("market"))
        ck.check("board", "main", it.get("board"))
        ck.check("board_label", "深主板", it.get("board_label"))
        ck.check("secid", "0.000993", it.get("secid"))
    # 名称搜索 + 创业板
    code, body, _ = api_get("/api/search?" + urllib.parse.urlencode({"q": "宁德"}))
    items = body["data"]["items"]
    ck.check("search 宁德 命中", True, len(items) >= 1)
    gem = next((x for x in items if x["code"] == "300750"), None)
    ck.check("宁德时代 board=gem", True, gem is not None and gem["board"] == "gem")
    if gem:
        ck.check("宁德时代 board_label=创业板", "创业板", gem["board_label"])
        ck.check("宁德时代 secid=0.300750", "0.300750", gem["secid"])
    # 拼音首字母（宁德 → nd）
    code, body, _ = api_get("/api/search?q=nd")
    ck.check("search 拼音 nd 200", 200, code)
    # 沪市
    code, body, _ = api_get("/api/search?q=600664")
    it = body["data"]["items"][0] if body["data"]["items"] else {}
    ck.check("600664 market=sh", "sh", it.get("market"))
    ck.check("600664 board_label=沪主板", "沪主板", it.get("board_label"))
    # 上限 8 条
    code, body, _ = api_get("/api/search?q=6")
    ck.check("search 结果 ≤8 条", True, len(body["data"]["items"]) <= 8)
    # 无结果
    code, body, _ = api_get("/api/search?q=zzzzzz")
    ck.check("search 无结果 200 + 空数组", True,
             code == 200 and body["data"]["items"] == [])
    # 名称 / 拼音覆盖抽样（契约：按代码/名称/拼音首字母搜索 A 股）
    samples = [
        ("宁德", "300750", "创业板名称搜索"),
        ("五粮液", "000858", "主板名称搜索"),
        ("贵州茅台", "600519", "主板名称搜索"),
        ("中芯国际", "688981", "科创板名称搜索（原 P1-1 回归）"),
        ("金山办公", "688111", "科创板名称搜索（原 P1-1 回归）"),
        ("寒武纪", "688256", "科创板名称搜索（原 P1-1 回归）"),
        ("zxgj", "688981", "科创板拼音首字母（原 P1-1 回归）"),
        ("mddl", "000993", "拼音首字母"),
    ]
    for q, want, label in samples:
        code, body, _ = api_get("/api/search?" + urllib.parse.urlencode({"q": q}))
        codes = [x["code"] for x in body["data"]["items"]]
        ck.check(f"{label} q={q} 命中 {want}", True, want in codes)
    # 北交所搜索项回归（原 P3-1）：应返回 board=bse / market=bj / board_label=北交所，
    # 且点击后 /api/triggers 仍返回 NOT_SUPPORTED（搜索可见、规则不支持，属预期）
    for q, label in (("920001", "北交所代码"), ("贝特瑞", "北交所名称"), ("830799", "原 83 段代码")):
        code, body, _ = api_get("/api/search?" + urllib.parse.urlencode({"q": q}))
        items = body["data"]["items"]
        bse = [x for x in items if x.get("board") == "bse"]
        ck.check(f"{label} q={q} 返回北交所条目", True, len(bse) >= 1,
                 detail=f"实际={items}")
        if bse:
            ck.check(f"{label} board_label=北交所", "北交所", bse[0].get("board_label"))
            ck.check(f"{label} market=bj", "bj", bse[0].get("market"))
    # 北交所触发接口：搜索可见但规则不支持（NOT_SUPPORTED）
    code, body, _ = api_get("/api/triggers?code=920001")
    ck.check("北交所触发接口 NOT_SUPPORTED(400)", True,
             code == 400 and body.get("error", {}).get("code") == "NOT_SUPPORTED")
    # 北交所代码搜索项点击后由前端调用 /api/triggers?code=<bse code>，应得到 NOT_SUPPORTED
    # 参数缺失 / 空白
    for q in ("/api/search", "/api/search?q=", "/api/search?q=%20"):
        code, body, _ = api_get(q)
        ck.check(f"{q} -> 400 BAD_CODE", True,
                 code == 400 and body.get("error", {}).get("code") == "BAD_CODE")


# ------------------------------ 2. 错误码 ------------------------------ #
def check_errors(ck: Ck, findings: list[str]) -> None:
    ck.section("[3] 错误码")
    cases = [
        ("/api/triggers", 400, "BAD_CODE", "缺 code"),
        ("/api/triggers?code=", 400, "BAD_CODE", "空 code"),
        ("/api/triggers?code=abc", 400, "BAD_CODE", "乱码 code"),
        ("/api/triggers?code=000993abc", 400, "BAD_CODE", "超长 code"),
        ("/api/triggers?code=830799", 400, "NOT_SUPPORTED", "北交所(原83段)"),
        ("/api/triggers?code=920001", 400, "NOT_SUPPORTED", "北交所(920)"),
        ("/api/triggers?code=999999", 404, "NOT_FOUND", "不存在代码(原P2-1回归)"),
        ("/api/triggers?code=600001", 404, "NOT_FOUND", "已退市代码(原P2-2回归)"),
        ("/api/triggers?code=900901", 400, "NOT_SUPPORTED", "沪 B 股"),
        ("/api/triggers?code=200002", 400, "NOT_SUPPORTED", "深 B 股"),
        ("/api/triggers?code=000993&at=bad", 400, "BAD_CODE", "非法 at"),
        ("/api/triggers?code=000993&price=abc", 400, "BAD_CODE", "非法 price"),
        ("/api/triggers?code=000993&index_price=abc", 400, "BAD_CODE", "非法 index_price"),
    ]
    for path, exp_code, exp_err, label in cases:
        code, body, _ = api_get(path)
        err = body.get("error", {}) if isinstance(body, dict) else {}
        ck.check(f"{label}: HTTP {exp_code}", exp_code, code)
        ck.check(f"{label}: error.code={exp_err}", exp_err, err.get("code"))
        ck.check(f"{label}: ok=false", False, body.get("ok") if isinstance(body, dict) else None)
        ck.check(f"{label}: message 非空", True, bool(err.get("message")))
    # 契约要求：查不到 -> NOT_FOUND(404)，已在上方用例严格断言（P2 回归）


# ------------------------------ 3. 字段结构校验 ------------------------------ #
TOP_KEYS = {"stock", "index", "snapshot", "risk", "summary", "reset", "rows", "meta"}
STOCK_KEYS = {"code", "name", "market", "board", "board_label", "secid", "price",
              "prev_close", "pct", "open", "high", "low", "status", "suspended"}
INDEX_KEYS = {"code", "name", "price", "prev_close", "pct"}
SNAP_KEYS = {"trade_date", "time", "phase", "is_trading_day", "show_t0", "updated_at"}
ROW_KEYS = {"key", "date", "weekday", "is_today", "trigger_pct", "trigger_price",
            "trigger_rule", "rules"}
RULE_KEYS = {"key", "label", "price", "pct", "binding"}
DEV_KEYS = {"value", "threshold", "remaining", "progress", "stock_change",
            "index_change", "window_start", "window_end", "base_close", "base_date",
            "trigger_price", "level", "label"}
SAME_KEYS = {"count", "threshold", "remaining", "progress", "level", "label", "rule",
             "last_event", "events"}


def check_structure(ck: Ck, tag: str, data: dict) -> None:
    ck.check(f"{tag} 顶层字段齐全", True, TOP_KEYS <= set(data))
    ck.check(f"{tag} stock 字段齐全", True, STOCK_KEYS <= set(data.get("stock", {})))
    ck.check(f"{tag} index 字段齐全", True, INDEX_KEYS <= set(data.get("index", {})))
    ck.check(f"{tag} snapshot 字段齐全", True, SNAP_KEYS <= set(data.get("snapshot", {})))
    ck.check(f"{tag} summary 三卡齐全", True,
             {"dev10", "dev30", "same_dir"} <= set(data.get("summary", {})))
    for k in ("dev10", "dev30"):
        ck.check(f"{tag} summary.{k} 字段齐全", True,
                 DEV_KEYS <= set(data["summary"][k]))
    ck.check(f"{tag} summary.same_dir 字段齐全", True,
             SAME_KEYS <= set(data["summary"]["same_dir"]))
    ck.check(f"{tag} risk 字段齐全", True, {"level", "label", "reasons"} <= set(data.get("risk", {})))
    ck.check(f"{tag} reset 字段", True,
             {"reset10", "reset30", "last_trigger"} <= set(data.get("reset", {})))
    ck.check(f"{tag} meta 字段齐全", True,
             {"index_label", "row_horizon", "data_source", "data_note",
              "disclaimer"} <= set(data.get("meta", {})))
    ck.check(f"{tag} rows 10 行", 10, len(data.get("rows", [])))
    for i, r in enumerate(data.get("rows", [])):
        ck.check(f"{tag} rows[{i}] 字段齐全", True, ROW_KEYS <= set(r))
        ck.check(f"{tag} rows[{i}] rules 2~3 条", True, 2 <= len(r.get("rules", [])) <= 3)
        for rule in r.get("rules", []):
            ck.check(f"{tag} rows[{i}].{rule.get('key')} 字段齐全", True,
                     RULE_KEYS <= set(rule))
    # 类型
    st = data["stock"]
    for f in ("price", "prev_close", "pct", "open", "high", "low"):
        ck.check(f"{tag} stock.{f} 为数字", True, isinstance(st.get(f), (int, float)))
    ck.check(f"{tag} stock.status 合法", True,
             st.get("status") in ("trading", "auction", "pre_open", "closed", "suspended"))
    ck.check(f"{tag} stock.suspended 为 bool", True, isinstance(st.get("suspended"), bool))


# ------------------------------ 4. 内部一致性 ------------------------------ #
def check_internal(ck: Ck, tag: str, data: dict) -> None:
    st, idx, snap, summ = data["stock"], data["index"], data["snapshot"], data["summary"]
    rows, risk, meta = data["rows"], data["risk"], data["meta"]
    P = st["price"]

    # pct 与价格一致
    if st["prev_close"]:
        ck.check(f"{tag} stock.pct 与价格一致", True,
                 approx(st["pct"], (st["price"] / st["prev_close"] - 1) * 100))
    if idx["prev_close"]:
        ck.check(f"{tag} index.pct 与点位一致", True,
                 approx(idx["pct"], (idx["price"] / idx["prev_close"] - 1) * 100))

    # 行编号/日期/周几/is_today
    expect_first = "T+0" if snap["show_t0"] else "T+1"
    ck.check(f"{tag} 首行编号 {expect_first}", expect_first, rows[0]["key"])
    ck.check(f"{tag} 末行编号", "T+9" if snap["show_t0"] else "T+10", rows[-1]["key"])
    t0 = datetime.strptime(snap["trade_date"], "%Y-%m-%d")
    ck.check(f"{tag} trade_date 周几与 phase", True, t0.weekday() < 5 or snap["phase"] == "holiday")
    prev_date = ""
    for i, r in enumerate(rows):
        d = datetime.strptime(r["date"], "%Y-%m-%d")
        ck.check(f"{tag} {r['key']} weekday", V.WEEKDAY_CN[d.weekday()], r["weekday"])
        ck.check(f"{tag} {r['key']} 日期递增", True, r["date"] > prev_date)
        prev_date = r["date"]
        ck.check(f"{tag} {r['key']} is_today 正确", True,
                 r["is_today"] == (snap["show_t0"] and i == 0))
    if snap["show_t0"]:
        ck.check(f"{tag} 首行为当前交易日", snap["trade_date"], rows[0]["date"])
        ck.check(f"{tag} show_t0 时 phase 非 closed/holiday", True,
                 snap["phase"] in ("trading", "lunch", "auction", "pre_open"))

    # 每行 binding / trigger
    for r in rows:
        rules = r["rules"]
        keys = [x["key"] for x in rules]
        ck.check(f"{tag} {r['key']} 必含 10d/30d", True,
                 {"10d", "30d"} <= set(keys))
        ck.check(f"{tag} {r['key']} key 不重复", len(keys), len(set(keys)))
        ref = st["prev_close"] if r["key"] == "T+0" else P
        binds = [x for x in rules if x["binding"]]
        ck.check(f"{tag} {r['key']} binding 恰好 1 条（10d/30d 内）", 1,
                 len([x for x in rules if x["key"] in ("10d", "30d") and x["binding"]]))
        # binding 决定 trigger_price / trigger_rule / trigger_pct
        if binds:
            b = binds[0]
            ck.check(f"{tag} {r['key']} trigger_price=最低规则价", True,
                     approx(r["trigger_price"], min(x["price"] for x in rules
                                                    if x["key"] in ("10d", "30d")), 1e-9))
            ck.check(f"{tag} {r['key']} trigger_rule=binding key", b["key"], r["trigger_rule"])
            ck.check(f"{tag} {r['key']} trigger_price=binding price", True,
                     approx(r["trigger_price"], b["price"], 1e-9))
        ptol = 0.005 * 100.0 / abs(ref) + 0.011
        ck.check(f"{tag} {r['key']} trigger_pct 与价格一致", True,
                 approx(r["trigger_pct"], (r["trigger_price"] / ref - 1) * 100, ptol))
        for x in rules:
            ck.check(f"{tag} {r['key']}.{x['key']} pct 与 price 一致", True,
                     approx(x["pct"], (x["price"] / ref - 1) * 100, ptol))

    # summary 与 rows / 公式一致
    for k in ("dev10", "dev30"):
        s = summ[k]
        ck.check(f"{tag} {k}.value=stock-change - index_change", True,
                 approx(s["value"], s["stock_change"] - s["index_change"]))
        ck.check(f"{tag} {k}.remaining=threshold-value", True,
                 approx(s["remaining"], s["threshold"] - s["value"]))
        ck.check(f"{tag} {k}.progress=value/threshold", True,
                 approx(s["progress"], s["value"] / s["threshold"], 0.001))
        ck.check(f"{tag} {k}.window_start=base_date", s["window_start"], s["base_date"])
        ck.check(f"{tag} {k}.window_end=trade_date", snap["trade_date"], s["window_end"])
        i_base = _base_index_price(data, k)
        if i_base:
            expect_tp = V.r2(s["base_close"] * (s["threshold"] / 100.0
                                                + idx["price"] / i_base))
            ck.check(f"{tag} {k}.trigger_price 公式一致", expect_tp, s["trigger_price"], 0.02)
        ck.check(f"{tag} {k}.level 合法", True, s["level"] in ("high", "watch", "safe"))
        ck.check(f"{tag} {k}.label 与 level 对应", _LEVEL_LABEL.get(s["level"]), s["label"])
        # 当前卡片触发价 = 现价窗口的触发价（可能不等于 T+1 行）
        ck.check(f"{tag} {k}.threshold 正确", 100.0 if k == "dev10" else 200.0,
                 s["threshold"])

    # same_dir
    sd = summ["same_dir"]
    ck.check(f"{tag} same_dir.count=events 数", len(sd["events"]), sd["count"])
    ck.check(f"{tag} same_dir.remaining=threshold-count", True,
             approx(sd["remaining"], sd["threshold"] - sd["count"]))
    ck.check(f"{tag} same_dir.progress=count/threshold", True,
             approx(sd["progress"], sd["count"] / sd["threshold"], 0.001))
    ck.check(f"{tag} same_dir.level 合法", True, sd["level"] in ("high", "watch", "safe"))
    if sd["last_event"]:
        ck.check(f"{tag} same_dir.last_event=events[-1]",
                 (sd["last_event"]["start"], sd["last_event"]["end"]),
                 (sd["events"][-1]["start"], sd["events"][-1]["end"]))
        last = sd["events"][-1]
        threshold_pct = 20.0 if st["board"] == "main" else 30.0
        ck.check(f"{tag} 最近同向异动涨幅≥{threshold_pct}%", True,
                 last["pct"] >= threshold_pct - 0.02)
    for ev in sd["events"]:
        ck.check(f"{tag} 同向异动 start<end", True, ev["start"] < ev["end"])
    # 红线与板块一致 + rule 文案
    if st["board"] in ("gem", "star"):
        ck.check(f"{tag} 双创同向红线=3", 3, sd["threshold"])
        ck.check(f"{tag} 双创 rule 含 +30%", True, "+30%" in sd["rule"])
        ck.check(f"{tag} 双创 rule 含 3 次", True, "3次" in sd["rule"])
    else:
        ck.check(f"{tag} 主板同向红线=4", 4, sd["threshold"])
        ck.check(f"{tag} 主板 rule 含 +20%", True, "+20%" in sd["rule"])
        ck.check(f"{tag} 主板 rule 含 4 次", True, "4次" in sd["rule"])

    # risk / meta
    ck.check(f"{tag} risk.level 合法", True, risk["level"] in ("high", "watch", "safe"))
    ck.check(f"{tag} risk.label 对应", _LEVEL_LABEL.get(risk["level"]), risk["label"])
    ck.check(f"{tag} risk.reasons ≤3", True, len(risk["reasons"]) <= 3)
    ck.check(f"{tag} meta.row_horizon=10", 10, meta["row_horizon"])
    ck.check(f"{tag} meta.index_label 与指数名一致", True,
             meta["index_label"] in (idx["name"], idx["name"].replace("指数", "")))
    ck.check(f"{tag} meta.data_source 非空", True, bool(meta["data_source"]))
    ck.check(f"{tag} meta.data_source 含「东方财富」与「同花顺」", True,
             "东方财富" in meta["data_source"] and "同花顺" in meta["data_source"])
    ck.check(f"{tag} meta.disclaimer 非空", True, bool(meta["disclaimer"]))


_LEVEL_LABEL = {"high": "高危", "watch": "关注", "safe": "安全"}
# 快照 A 同向异动线：截图（一日滞后）= 21.71,21.71,21.71,23.72；后端确认的自然口径如下
_NATURAL_SAME = {0: 21.71, 1: 21.71, 2: 23.72, 3: 23.72}
_BASE_INDEX_CACHE: dict[str, dict[str, float]] = {}


def _base_index_price(data: dict, kind: str):
    """从数据源独立取 base 日指数收盘（用于校验 trigger_price 公式）。"""
    s = data["summary"][kind]
    key = data["index"]["code"]
    if key not in _BASE_INDEX_CACHE:
        return None
    series = _BASE_INDEX_CACHE[key]
    return series.get(s["base_date"])


def register_base_index(code: str, series) -> None:
    _BASE_INDEX_CACHE[code] = dict(series.ic if hasattr(series, "ic") else series)


# ------------------------------ 5. 快照回放比对 ------------------------------ #
def check_replay(ck: Ck, tag: str, snap_spec: dict, path: str) -> dict:
    code, body, _ = api_get(path)
    ck.check(f"{tag} HTTP 200", 200, code)
    data = body["data"]
    exp = snap_spec["expected"]
    check_structure(ck, tag, data)
    check_internal(ck, tag, data)

    rows = data["rows"]
    ck.check(f"{tag} 行数", 10, len(rows))
    for k, r in enumerate(rows):
        ck.check(f"{tag} {r['key']} 日期", exp["dates"][k], r["date"])
        ck.check(f"{tag} {r['key']} 触发涨幅%", exp["trigger_pct"][k], r["trigger_pct"], V.TOL)
        ck.check(f"{tag} {r['key']} 触发价", exp["trigger_price"][k], r["trigger_price"], V.TOL)
        rr = {x["key"]: x for x in r["rules"]}
        ck.check(f"{tag} {r['key']} 10日线", exp["line10"][k], rr["10d"]["price"], V.TOL)
        ck.check(f"{tag} {r['key']} 30日线", exp["line30"][k], rr["30d"]["price"], V.TOL)
        if "same" in exp:
            # 截图值为「一日滞后」口径；后端已确认保持自然口径，这里按自然口径断言
            natural = _NATURAL_SAME.get(k)  # None = 不显示
            got = rr["same"]["price"] if "same" in rr else None
            ck.check(f"{tag} {r['key']} 同向线(自然口径)", natural, got, V.TOL)
    # 快照时间/阶段/展示 T+0
    ck.check(f"{tag} trade_date", snap_spec["date"], data["snapshot"]["trade_date"])
    ck.check(f"{tag} time", snap_spec["time"], data["snapshot"]["time"])
    ck.check(f"{tag} phase", "trading" if snap_spec["show_t0"] else "closed",
             data["snapshot"]["phase"])
    ck.check(f"{tag} show_t0", snap_spec["show_t0"], data["snapshot"]["show_t0"])
    ck.check(f"{tag} stock.price", snap_spec["price"], data["stock"]["price"])
    ck.check(f"{tag} index.price", snap_spec.get("index_now"), data["index"]["price"], 0.02)
    # base
    if "base10" in exp:
        ck.check(f"{tag} 10日 base 日", exp["base10"][0], data["summary"]["dev10"]["base_date"])
        ck.check(f"{tag} 10日 base 收盘", exp["base10"][1],
                 data["summary"]["dev10"]["base_close"], 0.001)
    if "base30" in exp:
        ck.check(f"{tag} 30日 base 日", exp["base30"][0], data["summary"]["dev30"]["base_date"])
        ck.check(f"{tag} 30日 base 收盘", exp["base30"][1],
                 data["summary"]["dev30"]["base_close"], 0.001)
    # B: 清零 / 同向次数
    if "reset10" in exp:
        ck.check(f"{tag} reset10", exp["reset10"], data["reset"]["reset10"])
        lt = data["reset"]["last_trigger"] or {}
        ck.check(f"{tag} last_trigger.date", exp["last_trigger"]["date"], lt.get("date"))
        ck.check(f"{tag} last_trigger.kind", exp["last_trigger"]["kind"], lt.get("kind"))
        ck.check(f"{tag} last_trigger.value", exp["last_trigger"]["value"],
                 lt.get("value"), V.TOL)
        sd = data["summary"]["same_dir"]
        ck.check(f"{tag} 同向次数", exp["same_count"], sd["count"])
        ck.check(f"{tag} 同向红线", exp["same_threshold"], sd["threshold"])
        le = sd["last_event"] or {}
        ck.check(f"{tag} 最近同向 start", exp["last_event"]["start"], le.get("start"))
        ck.check(f"{tag} 最近同向 end", exp["last_event"]["end"], le.get("end"))
        ck.check(f"{tag} 最近同向 pct", exp["last_event"]["pct"], le.get("pct"), V.TOL)
    if snap_spec["code"] == "000993":
        ck.check(f"{tag} reset10=null", None, data["reset"]["reset10"])
        ck.check(f"{tag} reset30=null", None, data["reset"]["reset30"])
    return data


# ------------------------------ 6. 实时行情：独立引擎逐行比对 ------------------------------ #
def check_live(ck: Ck, tag: str, code: str) -> dict:
    """拉实时接口，与 QA 独立引擎（东财前复权日 K + 实时价）逐行/逐卡比对。"""
    c, body, _ = api_get(f"/api/triggers?code={code}")
    ck.check(f"{tag} HTTP 200", 200, c)
    data = body["data"]
    check_structure(ck, tag, data)
    check_internal(ck, tag, data)
    st, snap = data["stock"], data["snapshot"]
    # 独立取数 + 构造快照
    stock = V.load_em(st["secid"], 1)
    idx_secid = "0." + data["index"]["code"] if data["index"]["code"].startswith("3") \
        else "1." + data["index"]["code"]
    index = V.load_em(idx_secid, 0)
    eng = V.Engine(stock, index, st["market"], st["board"])
    register_base_index(data["index"]["code"], eng)
    # 未来 10 个交易日：本地 K 只到最近交易日，2026-11/12 之后无节假日，按工作日外推
    _extend_calendar(eng, through="2026-12-31")
    snap_spec = {"date": snap["trade_date"], "price": st["price"],
                 "prev_close": st["prev_close"], "show_t0": snap["show_t0"],
                 "market": st["market"], "board": st["board"]}
    mine = eng.build(snap_spec, data["index"]["price"])

    ck.check(f"{tag} 行数", 10, len(mine["rows"]))
    for i, (r, m) in enumerate(zip(data["rows"], mine["rows"])):
        ck.check(f"{tag} {r['key']} 日期一致", m.key, r["key"])
        ck.check(f"{tag} {r['key']} 行日期一致", m.date, r["date"])
        ck.check(f"{tag} {r['key']} 触发价一致", m.trigger_price, r["trigger_price"], 0.02)
        ck.check(f"{tag} {r['key']} 触发涨幅一致", m.trigger_pct, r["trigger_pct"], V.TOL)
        ck.check(f"{tag} {r['key']} 触发规则一致", m.trigger_rule, r["trigger_rule"])
        rr = {x["key"]: x for x in r["rules"]}
        mm = {x.key: x for x in m.rules}
        ck.check(f"{tag} {r['key']} 10日线一致", mm["10d"].price, rr["10d"]["price"], 0.02)
        ck.check(f"{tag} {r['key']} 30日线一致", mm["30d"].price, rr["30d"]["price"], 0.02)
    # summary 与独立复算
    for kind, mkind in (("dev10", "10d"), ("dev30", "30d")):
        s = data["summary"][kind]
        base = mine["base"][mkind]
        i_base = base["i_base"]
        dev = (st["price"] / base["base_close"] - 1 - (data["index"]["price"] / i_base - 1)) * 100
        ck.check(f"{tag} {kind}.value", round(dev, 2), s["value"], 0.02)
        ck.check(f"{tag} {kind}.base_date", base["base_date"], s["base_date"])
        ck.check(f"{tag} {kind}.base_close", round(base["base_close"], 2), s["base_close"], 0.005)
        ck.check(f"{tag} {kind}.trigger_price", base["trigger_price"], s["trigger_price"], 0.02)
        stock_change = (st["price"] / base["base_close"] - 1) * 100
        idx_change = (data["index"]["price"] / i_base - 1) * 100
        ck.check(f"{tag} {kind}.stock_change", round(stock_change, 2), s["stock_change"], 0.02)
        ck.check(f"{tag} {kind}.index_change", round(idx_change, 2), s["index_change"], 0.02)
        ck.check(f"{tag} {kind}.window_start", base["base_date"], s["window_start"])
    # 监管清零（独立重演）
    rep = mine["replay"]
    ck.check(f"{tag} reset10", rep.reset10, data["reset"]["reset10"])
    ck.check(f"{tag} reset30", rep.reset30, data["reset"]["reset30"])
    lt_api = data["reset"]["last_trigger"] or {}
    lt_mine = rep.last_trigger or {}
    ck.check(f"{tag} last_trigger.date", lt_mine.get("date"), lt_api.get("date"))
    ck.check(f"{tag} last_trigger.kind", lt_mine.get("kind"), lt_api.get("kind"))
    if lt_mine:
        ck.check(f"{tag} last_trigger.value", V.r2(lt_mine["value"]), lt_api.get("value"), V.TOL)
        ck.check(f"{tag} last_trigger.value≥阈值", True, lt_mine["value"] >= 100.0 - 1e-9)
    # 同向异动次数（独立扫描）
    sd_mine = eng.same_dir_count(snap_spec, data["index"]["price"])
    sd_api = data["summary"]["same_dir"]
    ck.check(f"{tag} 同向次数", sd_mine["count"], sd_api["count"])
    ck.check(f"{tag} 同向事件序列",
             [(e["start"], e["end"], e["pct"]) for e in sd_mine["events"]],
             [(e["start"], e["end"], e["pct"]) for e in sd_api["events"]])
    return data


def _extend_calendar(eng: "V.Engine", through: str) -> None:
    """本地日 K 只到最近交易日；按工作日外推未来交易日（2026-11~12 无节假日）。"""
    from datetime import date, timedelta
    last = eng.cal[-1]
    d = datetime.strptime(last, "%Y-%m-%d").date()
    end = datetime.strptime(through, "%Y-%m-%d").date()
    extra: list[str] = []
    while d < end:
        d += timedelta(days=1)
        if d.weekday() < 5:
            extra.append(d.strftime("%Y-%m-%d"))
    if extra:
        eng.cal = eng.cal + extra


# ------------------------------ 7. 交易阶段逻辑 ------------------------------ #
def check_phases(ck: Ck) -> None:
    ck.section("[7] 交易阶段 / 非交易日 / show_t0 逻辑")
    # 构造历史回放：同一天的不同时刻
    base = "/api/triggers?code=000993&price=19.77&index_price=2491.4&at="
    cases = [
        ("2026-09-18T08:00:00%2B08:00", "pre_open", True, True),
        ("2026-09-18T09:54:36%2B08:00", "trading", True, True),
        ("2026-09-18T12:00:00%2B08:00", "lunch", True, True),
        ("2026-09-18T15:30:00%2B08:00", "closed", True, False),
        ("2026-09-25T10:00:00%2B08:00", "holiday", False, False),   # 2026 中秋休市
    ]
    for at, phase, is_td, show_t0 in cases:
        code, body, _ = api_get(base + at)
        ck.check(f"phase@ {at[:19]} HTTP 200", 200, code)
        s = body["data"]["snapshot"]
        ck.check(f"phase@ {at[:19]} = {phase}", phase, s["phase"])
        ck.check(f"is_trading_day@ {at[:19]}", is_td, s["is_trading_day"])
        ck.check(f"show_t0@ {at[:19]}", show_t0, s["show_t0"])
        ck.check(f"updated_at@ {at[:19]} 回显", True,
                 s["updated_at"].startswith(at[:19].replace("T", "T")))
        if phase == "holiday":
            ck.check("非交易日 trade_date=最近交易日", "2026-09-24", s["trade_date"])
        if show_t0:
            ck.check(f"phase@{at[:19]} 行首=T+0", "T+0", body["data"]["rows"][0]["key"])
        else:
            ck.check(f"phase@{at[:19]} 行首=T+1", "T+1", body["data"]["rows"][0]["key"])
    # 当前实时（收盘后）
    code, body, _ = api_get("/api/triggers?code=000993")
    s = body["data"]["snapshot"]
    ck.check("实时 phase 为 closed（当前 18:xx）", True, s["phase"] in ("closed", "trading"))
    ck.check("实时 show_t0 与 phase 一致", True,
             s["show_t0"] == (s["phase"] != "closed" and s["phase"] != "holiday"))


# ------------------------------ 8. 前端静态资源 ------------------------------ #
def check_frontend(ck: Ck) -> None:
    ck.section("[8] 前端静态资源（零依赖 / 零 CDN）")
    import subprocess
    p = subprocess.run(["curl", "-sS", "--max-time", "20", BASE + "/"],
                       capture_output=True, text=True)
    html = p.stdout
    ck.check("GET / 返回 HTML", True, html.lstrip().startswith("<!DOCTYPE html"))
    ck.check("包含 viewport meta", True, 'name="viewport"' in html)
    ck.check("包含标题", True, "异动雷达" in html)
    # 零 CDN：不允许 http(s):// 外部脚本/样式引用
    import re
    ext = re.findall(r'(?:src|href)\s*=\s*["\'](https?://[^"\']+)', html)
    ck.check("零 CDN 外链（script/link）", 0, len(ext))
    # 本地静态资源可达
    assets = re.findall(r'(?:src|href)\s*=\s*["\'](/(?:web/)?[^"\'#?]+\.(?:js|css))', html)
    for a in sorted(set(assets)):
        c, _, _ = api_get(a)
        ck.check(f"静态资源 {a} 可达", 200, c)
    # API 路径在页面 JS 中（不强制）
    ck.check("页面包含 app 挂载点/脚本", True, ("app.js" in html) or ("<main" in html))


# ------------------------------ main ------------------------------ #
def main() -> int:
    global BASE
    for i, a in enumerate(sys.argv):
        if a == "--base" and i + 1 < len(sys.argv):
            BASE = sys.argv[i + 1]
    print("=" * 90)
    print(f"异动雷达 · 阶段 2 端到端验收  base={BASE}")
    print("=" * 90)
    findings: list[str] = []
    total = Ck()
    _last = [0, 0]

    def step(title: str) -> None:
        dp, df = total.pass_n - _last[0], total.fail_n - _last[1]
        _last[0], _last[1] = total.pass_n, total.fail_n
        print(f"  · {title}: PASS {dp} / FAIL {df}")

    check_health(total)
    step("health")
    check_search(total, findings)
    step("search")
    check_errors(total, findings)
    step("错误码")

    # 预取指数日 K（独立源），用于校验 summary.trigger_price = base_close*(阈值+I/I_base)
    for code_, secid_ in (("399106", "0.399106"), ("000001", "1.000001")):
        register_base_index(code_, {b.date: b.close for b in V.load_em(secid_, 0)})

    total.section("[4] 历史快照回放比对（参考截图 A/B）")
    spec_a = dict(V.SNAPSHOT_A)
    spec_a["index_now"] = 2491.4
    dA = check_replay(total, "A", spec_a,
                      "/api/triggers?code=000993&at=2026-09-18T09:54:36%2B08:00"
                      "&price=19.77&index_price=2491.4")
    step("回放 A")
    dB = check_replay(total, "B", V.SNAPSHOT_B,
                      "/api/triggers?code=600664&at=2026-08-13T18:20:05%2B08:00"
                      "&price=8.86&index_price=3926.96")
    step("回放 B")

    total.section("[5] 实时行情：与独立引擎逐行比对（深主板/沪主板/创业板/科创板）")
    d_rt = check_live(total, "实时000993(深主板)", "000993")
    d_sh = check_live(total, "实时600664(沪主板)", "600664")
    d_gem = check_live(total, "实时300750(创业板)", "300750")
    d_star = check_live(total, "实时688981(科创板)", "688981")
    step("实时逐行比对(4 板块)")
    total.check("科创板 688981 board=star", "star", d_star["stock"]["board"])
    total.check("科创板 688981 board_label=科创板", "科创板", d_star["stock"]["board_label"])
    total.check("科创板 同向红线=3", 3, d_star["summary"]["same_dir"]["threshold"])

    step("科创板附加断言")
    check_phases(total)
    step("交易阶段逻辑")
    check_frontend(total)
    step("前端静态资源")

    # 与参考截图差异（已知口径差异，不算 FAIL，只提示）
    print("\n" + "=" * 90)
    print("已知口径差异确认（与 Lead 约定一致，不算失败）")
    print("=" * 90)
    a_rows = {r["key"]: r for r in dA["rows"]}
    same_t2 = next((x["price"] for x in a_rows["T+2"]["rules"] if x["key"] == "same"), None)
    same_t3 = next((x["price"] for x in a_rows["T+3"]["rules"] if x["key"] == "same"), None)
    print(f"  A 同向线：T+2={same_t2}（截图 21.71，后端自然口径 23.72）、T+3={same_t3}（截图 23.72）")
    print(f"  A 同向线 T+4 起是否隐藏："
          f"{all('same' not in [x['key'] for x in r['rules']] for k, r in a_rows.items() if k in ('T+4','T+5','T+6'))}")
    print("  B 触发涨幅 T+6/T+10：后端 13.45/67.91（截图 13.44/67.90，舍入边界 +0.01）")

    # 汇总
    print("\n" + "=" * 90)
    print(f"总结果：PASS {total.pass_n} / FAIL {total.fail_n} / 共 {total.pass_n + total.fail_n}")
    print("=" * 90)
    if total.fails:
        print("\nFAIL 明细：")
        for item in total.fails:
            name, exp, act, diff = item[:4]
            detail = item[4] if len(item) > 4 else ""
            d = "" if diff is None else f" (diff={diff:+.4f})"
            print(f"  - {name}: 期望={exp!r} 实际={act!r}{d}"
                  + (f" :: {detail}" if detail else ""))
    if findings:
        print("\n观察项（建议改进，不阻塞）：")
        for f in findings:
            print("  -", f)
    return 0 if total.fail_n == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

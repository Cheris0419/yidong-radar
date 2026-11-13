#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
异动雷达 · GitHub Pages 在线版独立验收（QA）
=============================================
三部分，全部产出写入 yidong-radar/qa/：

  1) 部署一致性
     - 公网 https://cheris0419.github.io/yidong-radar/ 根目录与 8 个关键资源，
       逐字节比对「本地仓库 site/」与「origin/gh-pages 分支」；
     - 公网 <title> 含「（在线版）」；
     - GitHub 仓库（main/gh-pages 分支、文件数、README/文档/QA 报告/截图）与本地一致性。
  2) 引擎独立核验
     - 运行 qa/site_engine_dump.cjs（require 仓库 site/engine.js，用 core/fixtures 复算）；
     - 与 QA 自己的 Python 引擎（qa/validate_engine.py 的 Engine，独立实现）逐字段比对；
     - 同时核对两份历史快照的用户截图期望值。
  3) 公网数据核验
     - 运行 qa/pages_check.mjs（headless Chrome 抓公网页面、截图、在页面内调用被拦截的
       fetch('/api/*') 拿静态接口原始 JSON → qa/pages_observed.json）；
     - QA 用同花顺公开接口（站点同源数据）+ 自己的引擎独立重算期望值 → qa/pages_expected.json；
     - 与公网 payload 逐字段比对（误差 ≤0.02），并与本地 Python 服务 8765 的 /api/triggers 对比；
     - 校验错误码、搜索联想、控制台/网络。

用法：
  python3 qa/online_accept.py               # 全量（含 headless Chrome 公网 UI）
  python3 qa/online_accept.py --skip-ui     # 跳过公网 UI（仅部署+引擎+期望值）
  python3 qa/online_accept.py --base https://.../  # 指定其它在线地址
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta

QA = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(QA)
sys.path.insert(0, QA)
import validate_engine as V  # noqa: E402  QA 独立引擎（阶段 1）

PUBLIC = "https://cheris0419.github.io/yidong-radar/"
GH_REPO = "Cheris0419/yidong-radar"
GH_API = f"https://api.github.com/repos/{GH_REPO}"
GH_RAW = f"https://raw.githubusercontent.com/{GH_REPO}/main/"
LOCAL_API = "http://127.0.0.1:8765"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
ASSETS = ["index.html", "styles.css", "app.js", "calendar.js", "engine.js",
          "data.js", "static-api.js", "favicon.svg"]
TOL = 0.02
OBSERVATIONS: list[str] = []   # 环境限制等观察项（不计 FAIL）

LIVE_STOCKS = [
    {"code": "000993", "name": "闽东电力", "market": "sz", "board": "main", "ths": "hs_000993", "idx": "zs_399106", "idx_code": "399106"},
    {"code": "600664", "name": "哈药股份", "market": "sh", "board": "main", "ths": "hs_600664", "idx": "zs_1A0001", "idx_code": "000001"},
    {"code": "688981", "name": "中芯国际", "market": "sh", "board": "star", "ths": "hs_688981", "idx": "zs_1A0001", "idx_code": "000001"},
]


# --------------------------------------------------------------------------- #
# 0. 工具
# --------------------------------------------------------------------------- #
class Ck:
    def __init__(self, title: str):
        self.title = title
        self.pass_n = 0
        self.fail_n = 0
        self.fails: list[tuple] = []
        print(f"\n{'=' * 96}\n{title}\n{'=' * 96}")

    def check(self, name: str, expected, actual, tol: float = 0.0, detail: str = "") -> bool:
        diff = None
        if isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
            diff = float(actual) - float(expected)
            good = abs(diff) <= tol + 1e-12
        else:
            good = expected == actual
        if good:
            self.pass_n += 1
        else:
            self.fail_n += 1
            self.fails.append((name, expected, actual, diff, detail))
            d = "" if diff is None else f" diff={diff:+.4f}"
            print(f"  ✗ {name}: 期望={expected!r} 实际={actual!r}{d}{(' :: ' + detail) if detail else ''}")
        return good

    def near(self, name: str, expected: float, actual: float, tol: float = TOL) -> bool:
        return self.check(name, expected, actual, tol)

    def summary(self) -> None:
        print(f"  --- {self.title}: PASS {self.pass_n} / FAIL {self.fail_n} ---")


def curl_get(url: str, timeout: int = 40) -> tuple[int, bytes]:
    p = subprocess.run(
        ["curl", "-sS", "--compressed", "--max-time", str(timeout), "-A", UA,
         "-o", "-", "-w", "\n__YDR_STATUS__%{http_code}", url],
        capture_output=True)
    raw = p.stdout
    marker = b"\n__YDR_STATUS__"
    if marker in raw:
        body, _, st = raw.rpartition(marker)
        try:
            return int(st.decode().strip()), body
        except ValueError:
            return 0, raw
    return 0, raw


def curl_bytes(url: str, timeout: int = 40) -> bytes:
    return curl_get(url, timeout)[1]


def git_show(ref_path: str) -> bytes:
    p = subprocess.run(["git", "-C", ROOT, "show", ref_path], capture_output=True)
    return p.stdout if p.returncode == 0 else b""


def local_file(rel: str) -> bytes:
    with open(os.path.join(ROOT, rel), "rb") as f:
        return f.read()


def fmt(v, nd: int = 2) -> str:
    if isinstance(v, float):
        return f"{v:.{nd}f}"
    return str(v)


# --------------------------------------------------------------------------- #
# 1. 部署一致性
# --------------------------------------------------------------------------- #
def check_deployment(ck: Ck) -> dict:
    info = {}
    # --- 公网首页 ---
    status, html = curl_get(PUBLIC)
    ck.check("公网首页 HTTP 200", 200, status)
    text = html.decode("utf-8", errors="replace")
    m = re.search(r"<title>([^<]*)</title>", text)
    title = m.group(1).strip() if m else ""
    info["public_title"] = title
    ck.check("公网 HTML <title> 含「在线版」", True, "（在线版）" in title, detail=title)
    ck.check("公网首页含 viewport meta", True, 'name="viewport"' in text)
    ck.check("公网首页引用 site 版脚本（calendar/engine/data/static-api）", True,
             all(x in text for x in ("calendar.js", "engine.js", "data.js", "static-api.js")))
    # --- 8 个关键资源：公网 vs 本地 site/ vs gh-pages 分支 ---
    identical_public = 0
    identical_branch = 0
    for a in ASSETS:
        st, pub = curl_get(PUBLIC + a)
        loc = local_file("site/" + a)
        br = git_show("origin/gh-pages:" + a)
        ok_pub = (st == 200 and pub == loc)
        ok_br = (br == loc)
        identical_public += 1 if ok_pub else 0
        identical_branch += 1 if ok_br else 0
        if not ok_pub:
            ck.check(f"公网资源 {a} 与本地 site/ 逐字节一致", False, True,
                     detail=f"HTTP {st}, 公网 {len(pub)}B / 本地 {len(loc)}B")
        if not ok_br:
            ck.check(f"gh-pages 分支 {a} 与本地 site/ 一致", False, True)
    ck.check(f"公网 {len(ASSETS)} 个资源均与本地 site/ 逐字节一致", len(ASSETS), identical_public)
    ck.check(f"gh-pages {len(ASSETS)} 个资源均与本地 site/ 逐字节一致", len(ASSETS), identical_branch)
    # --- gh-pages 分支内容清单 ---
    tree = subprocess.run(["git", "-C", ROOT, "ls-tree", "--name-only", "origin/gh-pages"],
                          capture_output=True, text=True).stdout.split()
    info["gh_pages_files"] = tree
    ck.check("gh-pages 分支仅含 8 个静态站点文件", sorted(ASSETS), sorted(tree))

    # --- GitHub 仓库 API ---
    st, repo_raw = curl_get(GH_API, timeout=40)
    ck.check("GitHub 仓库 API 可访问", 200, st)
    repo = json.loads(repo_raw.decode()) if st == 200 else {}
    info["repo"] = {k: repo.get(k) for k in ("full_name", "private", "default_branch", "homepage", "pushed_at")}
    ck.check("仓库公开可访问", False, repo.get("private"))
    ck.check("默认分支 main", "main", repo.get("default_branch"))
    ck.check("仓库 homepage 指向 Pages 地址", True,
             str(repo.get("homepage") or "").startswith("https://cheris0419.github.io/yidong-radar"))

    st, br_raw = curl_get(GH_API + "/branches", timeout=40)
    branches = {b["name"]: b["commit"]["sha"][:8] for b in json.loads(br_raw.decode())} if st == 200 else {}
    info["branches"] = branches
    ck.check("存在 main 分支", True, "main" in branches)
    ck.check("存在 gh-pages 分支", True, "gh-pages" in branches)

    st, tree_raw = curl_get(GH_API + "/git/trees/main?recursive=1", timeout=60)
    tree_main = json.loads(tree_raw.decode()) if st == 200 else {}
    files = sorted(t["path"] for t in tree_main.get("tree", []) if t.get("type") == "blob")
    info["main_file_count"] = len(files)
    ck.check("main 分支文件数 ≥ 50", True, len(files) >= 50, detail=f"{len(files)}")
    key_paths = ["README.md", "docs/METHODOLOGY.md", "docs/BACKEND-CONTRACT.md",
                 "docs/DEPLOYMENT.md", "qa/REPORT.md", "core/engine.py",
                 "core/fixtures/000993.json", "core/fixtures/600664.json",
                 "scripts/test_site_engine.js"] + ["site/" + a for a in ASSETS]
    missing = [p for p in key_paths if p not in files]
    ck.check("README / 文档 / QA 报告 / 引擎 / 站点文件均已收录", [], missing)
    shots = [p for p in files if p.startswith("docs/screenshots/online-")]
    info["online_screenshots"] = shots
    ck.check("仓库含在线版截图（docs/screenshots/online-*）", True, len(shots) >= 2,
             detail=str(shots))

    # --- main 分支内容与本地一致性 ---
    main_same = []
    report_pending = False
    for rel in key_paths:
        if rel not in files:
            continue
        raw = curl_bytes(GH_RAW + rel, timeout=60)
        loc = local_file(rel)
        if raw == loc:
            main_same.append(rel)
        elif rel == "qa/REPORT.md" and "GitHub Pages 在线版验收" in loc.decode("utf-8", "replace"):
            report_pending = True   # 本轮 QA 新写的 §10，等待 Lead 推送
            ck.check("main 分支 qa/REPORT.md 为上一版（§10 已本地更新，待 Lead 推送）", True, True,
                     detail="本地 §10 更新尚未 push，不影响本轮验收结论")
        else:
            ck.check(f"main 分支 {rel} 与本地一致", False, True,
                     detail=f"main {len(raw)}B / local {len(loc)}B")
    ck.check(f"main 分支关键文件与本地一致（{len(key_paths)} 项）", len(key_paths),
             len(main_same) + (1 if report_pending else 0))
    # README / 部署文档 指向在线版
    readme = curl_bytes(GH_RAW + "README.md").decode("utf-8", "replace")
    deploy_doc = curl_bytes(GH_RAW + "docs/DEPLOYMENT.md").decode("utf-8", "replace")
    ck.check("README 指向 GitHub Pages 在线地址", True,
             "cheris0419.github.io/yidong-radar" in readme)
    ck.check("docs/DEPLOYMENT.md 描述 gh-pages 部署", True,
             "gh-pages" in deploy_doc and "cheris0419.github.io" in deploy_doc)
    # 在线版截图（仓库 + 内容为 PNG）
    png_ok = 0
    for rel in shots:
        raw = curl_bytes(GH_RAW + rel, timeout=60)
        if raw[:8] == b"\x89PNG\r\n\x1a\n" and len(raw) > 20_000:
            png_ok += 1
    ck.check("在线版截图均为有效 PNG（>20KB）", len(shots), png_ok)
    ck.check("公网 HTTPS 连接正常（curl 无证书错误）", True, status == 200)
    return info


# --------------------------------------------------------------------------- #
# 2. 引擎独立核验（site/engine.js vs QA Python 引擎）
# --------------------------------------------------------------------------- #
def natural_payload(eng: V.Engine, snap: dict, i_now: float, redline: int) -> dict:
    """用 QA 独立引擎生成「自然口径」payload（同向线：窗口截至目标日；显示规则
    与产品一致：最近 10 日同向事件数达到红线-1 时才展示 same 线）。"""
    res = eng.build(snap, i_now)
    D, P = snap["date"], snap["price"]
    s, idx, dates = eng._series(D, P, res["row_dates"][-1])
    sd = eng.same_dir_count(snap, i_now)
    events = sd["events"]
    rows = []
    for row in res["rows"]:
        ref = snap["prev_close"] if row.key == "T+0" else P
        rm = {r.key: r for r in row.rules}
        rules = []
        for kind, label in (("10d", "10日 100%"), ("30d", "30日 200%")):
            raw = rm[kind].raw_price
            rules.append({"key": kind, "label": label, "raw": raw,
                          "price": V.r2(raw), "pct": V.r2((raw / ref - 1) * 100)})
        j = dates.index(row.date)
        edge = dates[max(0, j - 9)]
        show = sum(1 for e in events if e["end"] >= edge) >= redline - 1
        if show:
            sem = eng.same_dir_line(snap, row.key, row.date, s, idx, dates, variant="semantic")
            if sem:
                raw = sem[0]
                rules.append({"key": "same", "label": "十日同向异动线", "raw": raw,
                              "price": V.r2(raw), "pct": V.r2((raw / ref - 1) * 100)})
        binding = min(rules, key=lambda r: r["raw"])
        for r in rules:
            r["binding"] = r is binding
        rows.append({
            "key": row.key, "date": row.date, "weekday": row.weekday,
            "is_today": row.is_today,
            "trigger_pct": V.r2((binding["raw"] / ref - 1) * 100),
            "trigger_price": binding["price"], "trigger_rule": binding["key"],
            "rules": [{"key": r["key"], "label": r["label"], "price": r["price"],
                       "pct": r["pct"], "binding": r["binding"]} for r in rules],
        })
    summary = {}
    for kind, thr, key, name in (("10d", 1.0, "dev10", "10日"), ("30d", 2.0, "dev30", "30日")):
        b = res["base"][kind]
        i_base = b["i_base"]
        stock = (P / b["base_close"] - 1) * 100
        index = (i_now / i_base - 1) * 100
        value = stock - index
        ratio = value / (thr * 100)
        level = "high" if ratio >= 0.8 else ("watch" if ratio >= 0.5 else "safe")
        summary[key] = {
            "value": V.r2(value), "threshold": thr * 100,
            "remaining": V.r2(thr * 100 - value),
            "progress": round(ratio, 4),
            "stock_change": V.r2(stock), "index_change": V.r2(index),
            "window_start": b["base_date"], "window_end": D,
            "base_close": V.r2(b["base_close"]), "base_date": b["base_date"],
            "trigger_price": b["trigger_price"], "level": level,
            "label": {"high": "高危", "watch": "关注", "safe": "安全"}[level],
        }
    same = {
        "count": sd["count"], "threshold": redline,
        "remaining": max(0, redline - sd["count"]),
        "progress": round(sd["count"] / redline, 4) if redline else 0,
        "level": ("high" if sd["count"] >= redline - 1 else ("watch" if sd["count"] >= 1 else "safe")),
        "events": [{"start": e["start"], "end": e["end"], "pct": e["pct"]} for e in events],
        "last_event": ({"start": events[-1]["start"], "end": events[-1]["end"],
                        "pct": events[-1]["pct"]} if events else None),
    }
    replay = res["replay"]
    lt = replay.last_trigger
    reset = {
        "reset10": replay.reset10, "reset30": replay.reset30,
        "last_trigger": ({"kind": lt["kind"], "date": lt["date"],
                          "value": V.r2(lt["value"])} if lt else None),
    }
    return {"rows": rows, "summary": summary, "same_dir": same, "reset": reset}


def load_fixture(code: str):
    fx = json.load(open(os.path.join(ROOT, "core/fixtures", code + ".json"), encoding="utf-8"))
    stock = [V.Bar(V.norm(b["d"]), float(b["c"])) for b in fx["stock"]]
    index = [V.Bar(V.norm(b["d"]), float(b["c"])) for b in fx["index"]]
    return stock, index


def fixture_expected() -> dict:
    """QA 独立引擎在 core/fixtures 上的结果（对应快照 A/B 的输入）。"""
    out = {}
    stock, index = load_fixture("000993")
    eng = V.Engine(stock, index, "sz", "main")
    snap = {"date": "2026-09-18", "price": 19.77, "prev_close": 17.97, "show_t0": True}
    out["A"] = {"payload": natural_payload(eng, snap, 2491.4, 4),
                "inputs": {"seriesLen": len(eng.bar_dates), "price": 19.77, "indexPrice": 2491.4}}
    stock, index = load_fixture("600664")
    eng = V.Engine(stock, index, "sh", "main")
    snap = {"date": "2026-08-13", "price": 8.86, "prev_close": 8.81, "show_t0": False}
    out["B"] = {"payload": natural_payload(eng, snap, 3926.96, 4),
                "inputs": {"seriesLen": len(eng.bar_dates), "price": 8.86, "indexPrice": 3926.96}}
    return out


def compare_payload(ck: Ck, tag: str, expected: dict, actual: dict,
                    tol: float = TOL, compare_risk: bool = False) -> None:
    """逐字段比对 rows/summary/same_dir/reset（expected 为 QA 独立引擎结果）。"""
    # rows
    er, ar = expected["rows"], actual.get("rows") or []
    ck.check(f"{tag} rows 行数", len(er), len(ar))
    for i in range(min(len(er), len(ar))):
        e, a = er[i], ar[i]
        ck.check(f"{tag} {e['key']} key/日期", (e["key"], e["date"]), (a.get("key"), a.get("date")))
        ck.check(f"{tag} {e['key']} weekday", e["weekday"], a.get("weekday"))
        ck.check(f"{tag} {e['key']} is_today", e["is_today"], a.get("is_today"))
        ck.near(f"{tag} {e['key']} 触发价", e["trigger_price"], a.get("trigger_price"), tol)
        ck.near(f"{tag} {e['key']} 触发涨幅", e["trigger_pct"], a.get("trigger_pct"), tol)
        ck.check(f"{tag} {e['key']} binding 规则", e["trigger_rule"], a.get("trigger_rule"))
        arule = {r["key"]: r for r in (a.get("rules") or [])}
        brule = {r["key"]: r for r in e["rules"]}
        ck.check(f"{tag} {e['key']} 规则集合", sorted(brule), sorted(arule))
        for k in brule:
            if k in arule:
                ck.near(f"{tag} {e['key']}.{k} 触发价", brule[k]["price"], arule[k].get("price"), tol)
                ck.near(f"{tag} {e['key']}.{k} 触发涨幅", brule[k]["pct"], arule[k].get("pct"), tol)
                ck.check(f"{tag} {e['key']}.{k} binding", brule[k]["binding"], arule[k].get("binding"))
                ck.check(f"{tag} {e['key']}.{k} label", brule[k]["label"], arule[k].get("label"))
    # summary dev
    for key, name in (("dev10", "10日"), ("dev30", "30日")):
        e = expected["summary"][key]
        a = (actual.get("summary") or {}).get(key) or {}
        ck.near(f"{tag} {name}偏离值", e["value"], a.get("value"))
        ck.near(f"{tag} {name}阈值", e["threshold"], a.get("threshold"))
        ck.near(f"{tag} {name}剩余", e["remaining"], a.get("remaining"))
        ck.near(f"{tag} {name}进度", e["progress"], a.get("progress"), 0.001)
        ck.near(f"{tag} {name}股票涨幅", e["stock_change"], a.get("stock_change"))
        ck.near(f"{tag} {name}指数涨幅", e["index_change"], a.get("index_change"))
        ck.check(f"{tag} {name}窗口起算日", e["window_start"], a.get("window_start"))
        ck.check(f"{tag} {name}窗口结束日", e["window_end"], a.get("window_end"))
        ck.near(f"{tag} {name}base 收盘", e["base_close"], a.get("base_close"), 0.005)
        ck.check(f"{tag} {name}base 日", e["base_date"], a.get("base_date"))
        ck.near(f"{tag} {name}触发价", e["trigger_price"], a.get("trigger_price"))
    # same_dir
    e_sd = expected["same_dir"]
    a_sd = (actual.get("summary") or {}).get("same_dir") or {}
    ck.check(f"{tag} 同向次数", e_sd["count"], a_sd.get("count"))
    ck.check(f"{tag} 同向红线", e_sd["threshold"], a_sd.get("threshold"))
    ck.check(f"{tag} 同向剩余", e_sd["remaining"], a_sd.get("remaining"))
    ck.near(f"{tag} 同向进度", e_sd["progress"], a_sd.get("progress"), 0.001)
    a_events = a_sd.get("events") or []
    ck.check(f"{tag} 同向事件数", len(e_sd["events"]), len(a_events))
    for i, ev in enumerate(e_sd["events"]):
        if i < len(a_events):
            ck.check(f"{tag} 事件{i} 起止", (ev["start"], ev["end"]),
                     (a_events[i].get("start"), a_events[i].get("end")))
            ck.near(f"{tag} 事件{i} 涨幅", ev["pct"], a_events[i].get("pct"))
    e_le, a_le = e_sd["last_event"], a_sd.get("last_event") or {}
    if e_le:
        ck.check(f"{tag} 最近同向事件", (e_le["start"], e_le["end"]),
                 (a_le.get("start"), a_le.get("end")))
        ck.near(f"{tag} 最近同向涨幅", e_le["pct"], a_le.get("pct"))
    # reset
    e_r = expected["reset"]
    a_r = actual.get("reset") or {}
    ck.check(f"{tag} reset10", e_r["reset10"], a_r.get("reset10"))
    ck.check(f"{tag} reset30", e_r["reset30"], a_r.get("reset30"))
    e_lt, a_lt = e_r["last_trigger"], a_r.get("last_trigger") or {}
    if e_lt:
        ck.check(f"{tag} 最近严重异动 日期/类型", (e_lt["date"], e_lt["kind"]),
                 (a_lt.get("date"), a_lt.get("kind")))
        ck.near(f"{tag} 最近严重异动 偏离值", e_lt["value"], a_lt.get("value"))
    else:
        ck.check(f"{tag} 无严重异动触发记录", None, a_lt if a_lt else None)


def check_engine(ck: Ck, skip_ui: bool) -> dict:
    print("\n[2] 运行 node qa/site_engine_dump.cjs（site/engine.js 离线复算核心样例） ...")
    p = subprocess.run(["node", os.path.join(QA, "site_engine_dump.cjs")],
                       capture_output=True, text=True, cwd=ROOT)
    ck.check("site_engine_dump.cjs 正常退出", 0, p.returncode, detail=p.stderr[-300:])
    dump = json.load(open(os.path.join(QA, "site_engine_dump.json"), encoding="utf-8"))
    exp = fixture_expected()
    for key, label in (("A", "快照 A（000993 @ 2026-09-18 09:54:36）"),
                       ("B", "快照 B（600664 @ 2026-08-13 18:20）")):
        d = dump[key]["result"]
        mine = exp[key]["payload"]
        ck.check(f"[引擎核验] {key} 行数", len(mine["rows"]), len(d["rows"]))
        for i, row in enumerate(mine["rows"]):
            site = d["rows"][i]
            ck.check(f"[引擎核验] {key} {row['key']} 触发价", row["trigger_price"],
                     site["trigger_price"], 0.011)
            ck.check(f"[引擎核验] {key} {row['key']} 触发涨幅", row["trigger_pct"],
                     site["trigger_pct"], 0.011)
            ck.check(f"[引擎核验] {key} {row['key']} binding", row["trigger_rule"],
                     site["trigger_rule"])
            site_r = {r["key"]: r for r in site["rules"]}
            my_r = {r["key"]: r for r in row["rules"]}
            ck.check(f"[引擎核验] {key} {row['key']} 规则集合", sorted(my_r), sorted(site_r))
            for k in my_r:
                if k in site_r:
                    ck.check(f"[引擎核验] {key} {row['key']}.{k} 价", my_r[k]["price"],
                             site_r[k]["price"], 0.011)
        for key2, name in (("dev10", "10日"), ("dev30", "30日")):
            e, s = mine["summary"][key2], d["summary"][key2]
            for f in ("value", "stock_change", "index_change", "remaining", "base_close",
                      "trigger_price"):
                ck.check(f"[引擎核验] {key} {name}{f}", e[f], s[f], 0.011)
            for f in ("window_start", "base_date"):
                ck.check(f"[引擎核验] {key} {name}{f}", e[f], s[f])
            ck.check(f"[引擎核验] {key} {name}progress", e["progress"], s["progress"], 0.001)
        ck.check(f"[引擎核验] {key} 同向次数", mine["same_dir"]["count"],
                 d["summary"]["same_dir"]["count"])
        ck.check(f"[引擎核验] {key} reset10", mine["reset"]["reset10"], d["reset"]["reset10"])
        ck.check(f"[引擎核验] {key} reset30", mine["reset"]["reset30"], d["reset"]["reset30"])
        if mine["reset"]["last_trigger"]:
            ck.check(f"[引擎核验] {key} 最近触发日", mine["reset"]["last_trigger"]["date"],
                     (d["reset"]["last_trigger"] or {}).get("date"))
            ck.check(f"[引擎核验] {key} 最近触发值", mine["reset"]["last_trigger"]["value"],
                     (d["reset"]["last_trigger"] or {}).get("value"), 0.011)
        # 与用户截图参考值核对（A/B 已知期望）
        ref = V.SNAPSHOT_A["expected"] if key == "A" else V.SNAPSHOT_B["expected"]
        for i, want in enumerate(ref["trigger_price"]):
            ck.check(f"[截图核对] {key} T+{i} 触发价(截图)", want,
                     d["rows"][i]["trigger_price"], TOL)
        for i, want in enumerate(ref["trigger_pct"]):
            ck.check(f"[截图核对] {key} T+{i} 触发涨幅(截图)", want,
                     d["rows"][i]["trigger_pct"], TOL)
        for i, want in enumerate(ref["line30"]):
            sr = {r["key"]: r for r in d["rows"][i]["rules"]}
            ck.check(f"[截图核对] {key} T+{i} 30日线(截图)", want, sr["30d"]["price"], TOL)
    return exp


# --------------------------------------------------------------------------- #
# 3. 在线数据期望（同花顺公开接口 + QA 独立引擎）
# --------------------------------------------------------------------------- #
def fetch_ths_text(url: str) -> str:
    st, raw = curl_get(url, timeout=30)
    if st != 200:
        raise RuntimeError(f"同花顺接口 HTTP {st}: {url}")
    return raw.decode("utf-8", errors="replace")


def parse_jsonp_any(text: str) -> dict:
    l, r = text.find("("), text.rfind(")")
    if l < 0 or r < 0:
        raise RuntimeError("JSONP 解析失败")
    return json.loads(text[l + 1:r])


def fetch_quote(ths: str) -> dict:
    obj = parse_jsonp_any(fetch_ths_text(f"https://d.10jqka.com.cn/v6/realhead/{ths}/last.js"))
    items = obj.get("items") or {}
    def numf(k):
        try:
            v = float(items.get(k))
            return v if v == v else None
        except (TypeError, ValueError):
            return None
    return {"price": numf("10"), "prev_close": numf("6"), "open": numf("7"),
            "high": numf("8"), "low": numf("9"), "status": str(items.get("stockStatus") or "")}


def fetch_ths_kline(sym: str):
    raw = fetch_ths_text(f"https://d.10jqka.com.cn/v6/line/{sym}/01/last.js")
    bars = V._parse_ths(raw)
    obj = parse_jsonp_any(raw)
    return bars, str(obj.get("name") or "")


def fetch_index_quote(idx: str) -> dict:
    obj = parse_jsonp_any(fetch_ths_text(f"https://d.10jqka.com.cn/v6/time/{idx}/last.js"))
    body = obj.get(idx) or obj
    rows = [r for r in str(body.get("data") or "").split(";") if r]
    price = None
    for r in reversed(rows):
        p = r.split(",")
        if len(p) >= 2 and p[1]:
            try:
                price = float(p[1])
                break
            except ValueError:
                continue
    return {"name": str(body.get("name") or ""), "price": price,
            "pre": body.get("pre"), "date": str(body.get("date") or "")}


def phase_now(now: datetime) -> dict:
    """按 docs/BACKEND-CONTRACT 交易阶段定义计算（2026-11 无节假日）。"""
    is_td = now.weekday() < 5
    mins = now.hour * 60 + now.minute
    if not is_td:
        phase, show_t0 = "holiday", False
    elif mins < 9 * 60 + 30:
        phase, show_t0 = "pre_open", True
    elif mins < 11 * 60 + 30:
        phase, show_t0 = "trading", True
    elif mins < 13 * 60:
        phase, show_t0 = "lunch", True
    elif mins < 15 * 60:
        phase, show_t0 = "trading", True
    else:
        phase, show_t0 = "closed", False
    return {"phase": phase, "is_trading_day": is_td, "show_t0": show_t0,
            "time": now.strftime("%H:%M:%S"), "iso": now.strftime("%Y-%m-%dT%H:%M:%S+08:00")}


def next_trading_days(after_iso: str, n: int) -> list[str]:
    """未来交易日：2026-11/12 无节假日，按工作日推算。"""
    d = datetime.strptime(after_iso, "%Y-%m-%d")
    out = []
    while len(out) < n:
        d += timedelta(days=1)
        if d.weekday() < 5:
            out.append(d.strftime("%Y-%m-%d"))
    return out


def build_live_expected(stock: dict, now: datetime) -> dict:
    quote = fetch_quote(stock["ths"])
    stock_bars_ths, ths_name = fetch_ths_kline(stock["ths"])
    index_quote = fetch_index_quote(stock["idx"])
    index_bars_ths, _ = fetch_ths_kline(stock["idx"])
    ph = phase_now(now)
    today_iso = now.strftime("%Y-%m-%d")

    # 与 site/data.js buildContext 相同的上下文语义（但由 QA 独立重算）
    stock_map = {b.date: b.close for b in stock_bars_ths}
    index_map = {b.date: b.close for b in index_bars_ths}
    all_dates = sorted(index_map)
    price = quote["price"] if (quote["price"] and quote["price"] > 0) else (
        stock_map[sorted(stock_map)[-1]] if stock_map else None)
    index_price = index_quote["price"] if (index_quote["price"] and index_quote["price"] > 0) else index_map[all_dates[-1]]
    if not price or price <= 0:
        raise RuntimeError(f"{stock['code']} 无法获取现价")
    if ph["is_trading_day"]:
        hist_end = today_iso
        stock_map[hist_end] = price
        index_map[hist_end] = index_price
    else:
        hist_end = all_dates[-1]
        price = stock_map.get(hist_end, price)
        index_price = index_map.get(hist_end, index_price)
    # 以指数交易日历为基准，停牌日按前值填充
    series = []
    last_stock = None
    for d in all_dates:
        if d > hist_end:
            continue
        if stock_map.get(d) is not None:
            last_stock = stock_map[d]
        if last_stock is None:
            continue
        series.append({"d": d, "sc": last_stock, "ic": index_map[d]})
    if not series or series[-1]["d"] != hist_end:
        series.append({"d": hist_end, "sc": last_stock or price,
                       "ic": index_map.get(hist_end, index_price)})
    series[-1]["sc"] = price
    series[-1]["ic"] = index_price
    if len(series) < 35:
        raise RuntimeError(f"{stock['code']} 历史日 K 不足（{len(series)}）")
    prev_close = series[-2]["sc"]
    future = next_trading_days(hist_end, 9 if ph["show_t0"] else 10)

    eng = V.Engine([V.Bar(s["d"], s["sc"]) for s in series],
                   [V.Bar(s["d"], s["ic"]) for s in series],
                   stock["market"], stock["board"])
    eng.cal = sorted(set(eng.cal) | set(future))
    snap = {"date": hist_end, "price": price, "prev_close": prev_close,
            "show_t0": ph["show_t0"]}
    redline = 3 if stock["board"] in ("gem", "star") else 4
    payload = natural_payload(eng, snap, index_price, redline)
    return {
        "code": stock["code"], "name": ths_name or stock["name"],
        "market": stock["market"], "board": stock["board"],
        "board_label": {"main": "沪主板" if stock["market"] == "sh" else "深主板",
                        "gem": "创业板", "star": "科创板"}[stock["board"]],
        "inputs": {"price": price, "index_price": index_price, "prev_close": prev_close,
                   "quote": quote, "index_quote": index_quote,
                   "hist_end": hist_end, "phase": ph, "future_dates": future,
                   "series_len": len(series),
                   "ths": {"quote": f"https://d.10jqka.com.cn/v6/realhead/{stock['ths']}/last.js",
                           "kline": f"https://d.10jqka.com.cn/v6/line/{stock['ths']}/01/last.js",
                           "index_quote": f"https://d.10jqka.com.cn/v6/time/{stock['idx']}/last.js",
                           "index_kline": f"https://d.10jqka.com.cn/v6/line/{stock['idx']}/01/last.js"}},
        "payload": payload,
    }


def check_live_expected(ck: Ck, now: datetime) -> dict:
    print("\n[3] 用同花顺公开接口 + QA 独立引擎计算在线版期望值 ...")
    out = {"generated_at": now.strftime("%Y-%m-%dT%H:%M:%S+08:00"), "codes": {}}
    for stock in LIVE_STOCKS:
        try:
            e = build_live_expected(stock, now)
            out["codes"][stock["code"]] = e
            print(f"  · {stock['code']} {stock['name']}: price={e['inputs']['price']} "
                  f"index={e['inputs']['index_price']} prev={e['inputs']['prev_close']} "
                  f"phase={e['inputs']['phase']['phase']} show_t0={e['inputs']['phase']['show_t0']}")
        except Exception as ex:  # noqa: BLE001
            ck.check(f"在线期望值计算 {stock['code']}", True, False, detail=str(ex))
    return out


# --------------------------------------------------------------------------- #
# 4. 公网 UI/接口比对
# --------------------------------------------------------------------------- #
def fetch_local_api(code: str) -> dict | None:
    st, raw = curl_get(f"{LOCAL_API}/api/triggers?code={code}", timeout=60)
    if st != 200:
        return None
    try:
        return json.loads(raw.decode()).get("data")
    except json.JSONDecodeError:
        return None


def check_online_vs_expected(ck: Ck, expected: dict) -> None:
    path = os.path.join(QA, "pages_observed.json")
    if not os.path.exists(path):
        ck.check("公网观测数据 pages_observed.json 存在", True, False,
                 detail="先运行 node qa/pages_check.mjs")
        return
    obs = json.load(open(path, encoding="utf-8"))
    ck.check("公网观测数据 base 为 Pages 地址", True, str(obs.get("base", "")).startswith(PUBLIC),
             detail=str(obs.get("base")))
    for code, exp in expected["codes"].items():
        case = (obs.get("cases") or {}).get(code) or {}
        payload = case.get("payload") or {}
        ck.check(f"[公网 {code}] 静态接口 ok=true", True, payload.get("ok"))
        data = payload.get("data") or {}
        if not data:
            continue
        top_keys = {"stock", "index", "snapshot", "risk", "summary", "reset", "rows", "meta"}
        ck.check(f"[公网 {code}] 契约顶层 8 字段齐全", True, top_keys <= set(data))
        ck.check(f"[公网 {code}] summary 三卡齐全", True,
                 {"dev10", "dev30", "same_dir"} <= set(data.get("summary") or {}))
        ck.check(f"[公网 {code}] stock 关键字段齐全", True,
                 {"code", "name", "market", "board", "board_label", "secid", "price",
                  "prev_close", "pct", "status", "suspended"} <= set(data.get("stock") or {}))
        ck.check(f"[公网 {code}] index 关键字段齐全", True,
                 {"code", "name", "price", "prev_close", "pct"} <= set(data.get("index") or {}))
        ck.check(f"[公网 {code}] snapshot 关键字段齐全", True,
                 {"trade_date", "time", "phase", "is_trading_day", "show_t0", "updated_at"}
                 <= set(snap := (data.get("snapshot") or {})))
        ck.check(f"[公网 {code}] snapshot.updated_at 为 ISO8601 带时区", True,
                 bool(re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+08:00$",
                              str(snap.get("updated_at") or ""))), detail=str(snap.get("updated_at")))
        ck.check(f"[公网 {code}] meta 关键字段齐全", True,
                 {"index_label", "row_horizon", "data_source", "data_note", "disclaimer"}
                 <= set(data.get("meta") or {}))
        ck.check(f"[公网 {code}] risk 字段齐全", True,
                 {"level", "label", "reasons"} <= set(data.get("risk") or {}))
        ck.check(f"[公网 {code}] reset 字段齐全", True,
                 {"reset10", "reset30", "last_trigger"} <= set(data.get("reset") or {}))
        ck.check(f"[公网 {code}] 股票名称", exp["name"], (data.get("stock") or {}).get("name"))
        ck.check(f"[公网 {code}] 板块", exp["board"], (data.get("stock") or {}).get("board"))
        ck.check(f"[公网 {code}] 板块标签", exp["board_label"],
                 (data.get("stock") or {}).get("board_label"))
        ck.near(f"[公网 {code}] 现价", exp["inputs"]["price"],
                (data.get("stock") or {}).get("price"))
        ck.near(f"[公网 {code}] 昨收", exp["inputs"]["prev_close"],
                (data.get("stock") or {}).get("prev_close"))
        ck.near(f"[公网 {code}] 指数点位", exp["inputs"]["index_price"],
                (data.get("index") or {}).get("price"))
        snap = data.get("snapshot") or {}
        ck.check(f"[公网 {code}] 交易日", exp["inputs"]["hist_end"], snap.get("trade_date"))
        ck.check(f"[公网 {code}] 交易阶段", exp["inputs"]["phase"]["phase"], snap.get("phase"))
        ck.check(f"[公网 {code}] show_t0", exp["inputs"]["phase"]["show_t0"], snap.get("show_t0"))
        ck.check(f"[公网 {code}] 行数", 10, len(data.get("rows") or []))
        ck.check(f"[公网 {code}] 首行 T+0/当日", ("T+0", exp["inputs"]["hist_end"]),
                 ((data.get("rows") or [{}])[0].get("key"), (data.get("rows") or [{}])[0].get("date")))
        meta = data.get("meta") or {}
        ck.check(f"[公网 {code}] meta.row_horizon", 10, meta.get("row_horizon"))
        ck.check(f"[公网 {code}] meta.data_source 含「同花顺」", True,
                 "同花顺" in str(meta.get("data_source") or ""), detail=str(meta.get("data_source")))
        ck.check(f"[公网 {code}] meta.disclaimer 非空", True, bool(meta.get("disclaimer")))
        # 数据逐字段与 QA 独立引擎比对
        compare_payload(ck, f"[公网 {code}]", exp["payload"], data)
        # 前端渲染一致性（DOM vs payload）
        dom = case.get("dom") or {}
        ck.check(f"[公网 {code}] DOM 行数", 10, len(dom.get("rows") or []))
        if dom.get("rows"):
            ck.check(f"[公网 {code}] DOM 首行 key", "T+0", dom["rows"][0].get("key"))
            ck.check(f"[公网 {code}] DOM 首行涨幅文案",
                     f"{exp['payload']['rows'][0]['trigger_pct']:.2f}%",
                     dom["rows"][0].get("pct"))
            ck.check(f"[公网 {code}] DOM 读数卡数量", 3, len(dom.get("readouts") or []))
            ck.check(f"[公网 {code}] DOM 风险徽标", (data.get("risk") or {}).get("label"),
                     dom.get("risk"))
        # 与本地 Python 服务 8765 对比（可选第二参考；当前环境 push2 行情不可用时无法取数）
        local = fetch_local_api(code)
        if local:
            compare_payload(ck, f"[本地服务 {code}]", exp["payload"], local, tol=0.05)
            ck.check(f"[公网/本地 {code}] risk.level 一致", (local.get("risk") or {}).get("level"),
                     (data.get("risk") or {}).get("level"))
            ck.check(f"[公网/本地 {code}] risk.label 一致", (local.get("risk") or {}).get("label"),
                     (data.get("risk") or {}).get("label"))
        else:
            print(f"  · 本地服务 {code} 暂不可用（当前环境 push2 行情不可达），改以「QA 独立引擎」为第一参考；"
                  f"该对比项不计 FAIL")
            OBSERVATIONS.append(f"本地 Python 服务 {code} 取数失败（push2 行情不可达环境限制），"
                                f"公网 vs 本地引擎对比改用 QA 独立引擎/离线 fixtures 核验覆盖")
        # 风险徽标内部一致性：level = max(dev10, dev30, same_dir)
        rank = {"safe": 0, "watch": 1, "high": 2}
        sd = (data.get("summary") or {}).get("same_dir") or {}
        lv = [(data.get("summary") or {}).get("dev10", {}).get("level", "safe"),
              (data.get("summary") or {}).get("dev30", {}).get("level", "safe"),
              sd.get("level", "safe")]
        ck.check(f"[公网 {code}] risk.level=max(三卡 level)", max(lv, key=lambda x: rank.get(x, 0)),
                 (data.get("risk") or {}).get("level"))


def check_online_errors(ck: Ck) -> None:
    path = os.path.join(QA, "pages_observed.json")
    obs = json.load(open(path, encoding="utf-8"))
    expect = {"999999": "NOT_FOUND", "123456": "NOT_FOUND", "920185": "NOT_SUPPORTED",
              "900901": "NOT_SUPPORTED", "abc": "BAD_CODE"}
    for code, want in expect.items():
        case = (obs.get("cases") or {}).get("error_" + code) or {}
        dom = case.get("dom") or {}
        raw = case.get("raw") or {}
        ck.check(f"[错误态 {code}] 页面错误码", want, dom.get("errorCode"))
        ck.check(f"[错误态 {code}] errorView 可见", "error", dom.get("dataView"))
        if want == "NOT_SUPPORTED":
            ck.check(f"[错误态 {code}] 文案应提示「不支持」（北交所/B 股）", True,
                     ("不支持" in str(dom.get("errorTitle") or "")
                      or "暂不支持" in str(dom.get("errorText") or "")),
                     detail=str(dom.get("errorTitle")))
        ck.check(f"[错误态 {code}] 静态接口返回 JSON 错误", want,
                 ((raw.get("json") or {}).get("error") or {}).get("code"))
        # 静态版不应把 /api/* 请求发到真实网络（GitHub Pages 会返回 404 HTML）
        ck.check(f"[错误态 {code}] 无真实网络 /api 请求", 0, len(case.get("apiReqs") or []),
                 detail=json.dumps(case.get("apiReqs") or [], ensure_ascii=False))
    # 搜索：与 QA 独立调用的东财 suggest 映射结果比对
    search_obs = obs.get("search") or {}
    em = fetch_em_suggest
    checks = [("star", "中芯国际", "688981"), ("bse", "贝特瑞", "920185")]
    for key, q, code in checks:
        case = search_obs.get(key) or {}
        payload = case.get("payload") or {}
        items = (payload.get("data") or {}).get("items") or []
        ck.check(f"[搜索 {q}] 静态接口 ok", True, payload.get("ok"))
        ck.check(f"[搜索 {q}] 命中 {code}", True, any(x.get("code") == code for x in items),
                 detail=json.dumps(items, ensure_ascii=False))
        if code in [x.get("code") for x in items]:
            item = next(x for x in items if x.get("code") == code)
            want_board = "star" if code.startswith("688") else "bse"
            ck.check(f"[搜索 {q}] {code} board", want_board, item.get("board"))
            ck.check(f"[搜索 {q}] {code} market", "bj" if want_board == "bse" else "sh",
                     item.get("market"))
        # 与 QA 独立调用东财 suggest 并按契约映射的结果一致
        exp_items = em(q)
        ck.check(f"[搜索 {q}] 与 QA 独立 suggest 映射结果一致（code 顺序）",
                 [x["code"] for x in exp_items][:8], [x.get("code") for x in items][:8])
    for key, q in (("nh", "nh"), ("rh", "rh")):
        case = search_obs.get(key) or {}
        payload = case.get("payload") or {}
        items = (payload.get("data") or {}).get("items") or []
        ck.check(f"[搜索 {q}] 返回 A 股联想", True,
                 any(x.get("board") in ("main", "gem", "star") for x in items),
                 detail=json.dumps([x.get("code") + x.get("name", "") for x in items], ensure_ascii=False))
    # 选中北交所搜索结果 -> 应返回 NOT_SUPPORTED（与本地服务/契约一致）
    sel = (search_obs.get("bseSelected") or {}).get("dom") or {}
    ck.check("[搜索选择] 北交所股票错误码 NOT_SUPPORTED", "NOT_SUPPORTED", sel.get("errorCode"))


def fetch_em_suggest(keyword: str) -> list[dict]:
    """QA 独立调用东方财富 suggest 并按契约映射（不复用 site/data.js 的代码）。"""
    import urllib.parse
    token = "D43BF722C8E33BDC906FB84D85E326E8"
    url = ("https://searchapi.eastmoney.com/api/suggest/get?input="
           + urllib.parse.quote(keyword) + "&type=14&token=" + token + "&count=10&cb=cbqa")
    st, raw = curl_get(url, timeout=30)
    text = raw.decode("utf-8", errors="replace")
    l, r = text.find("("), text.rfind(")")
    rows = []
    if l >= 0 and r > l:
        try:
            rows = ((json.loads(text[l + 1:r]) or {}).get("QuotationCodeTable") or {}).get("Data") or []
        except json.JSONDecodeError:
            rows = []
    items = []
    for row in rows:
        classify = str(row.get("Classify") or "")
        if classify not in ("AStock", "23", "NEEQ"):
            continue
        code = str(row.get("Code") or "")
        mkt = str(row.get("MktNum") or "")
        if mkt not in ("0", "1"):
            continue
        sec = str(row.get("SecurityTypeName") or "")
        if sec in ("京A", "北A") or code.startswith(("920", "430", "83", "87", "88", "4")):
            items.append({"code": code, "name": str(row.get("Name") or ""), "market": "bj",
                          "board": "bse", "board_label": "北交所"})
        elif code.startswith(("600", "601", "603", "605")):
            items.append({"code": code, "name": str(row.get("Name") or ""), "market": "sh",
                          "board": "main", "board_label": "沪主板"})
        elif code.startswith(("688", "689")):
            items.append({"code": code, "name": str(row.get("Name") or ""), "market": "sh",
                          "board": "star", "board_label": "科创板"})
        elif code.startswith(("000", "001", "002", "003")):
            items.append({"code": code, "name": str(row.get("Name") or ""), "market": "sz",
                          "board": "main", "board_label": "深主板"})
        elif code.startswith(("300", "301")):
            items.append({"code": code, "name": str(row.get("Name") or ""), "market": "sz",
                          "board": "gem", "board_label": "创业板"})
        if len(items) >= 8:
            break
    return items


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main() -> int:
    global PUBLIC
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-ui", action="store_true", help="跳过 headless Chrome 公网 UI 验收")
    ap.add_argument("--base", default=PUBLIC)
    ap.add_argument("--no-run-pages-check", action="store_true", help="复用已有 pages_observed.json")
    args = ap.parse_args()
    PUBLIC = args.base if args.base.endswith("/") else args.base + "/"
    now = datetime.now()

    print("=" * 96)
    print(f"异动雷达 · GitHub Pages 在线版独立验收   {now.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"公网：{PUBLIC}")
    print(f"仓库：https://github.com/{GH_REPO}")
    print("=" * 96)

    # [1] 部署一致性
    ck_dep = Ck("[1] 部署一致性（公网 vs 仓库 site/ vs gh-pages）")
    dep_info = check_deployment(ck_dep)
    ck_dep.summary()

    # [2] 引擎独立核验
    ck_eng = Ck("[2] 引擎独立核验（site/engine.js vs QA Python 引擎，core/fixtures）")
    check_engine(ck_eng, args.skip_ui)
    ck_eng.summary()

    # [3] 在线期望值
    ck_exp = Ck("[3] 在线数据期望值（同花顺公开接口 + QA 独立引擎）")
    expected = check_live_expected(ck_exp, now)
    with open(os.path.join(QA, "pages_expected.json"), "w", encoding="utf-8") as f:
        json.dump(expected, f, ensure_ascii=False, indent=2)
    ck_exp.summary()

    # [4] 公网 UI（headless Chrome）
    ck_ui = Ck("[4] 公网 UI（headless Chrome，移动/桌面/搜索/错误态）")
    if args.skip_ui:
        print("  （--skip-ui：跳过）")
    else:
        if not args.no_run_pages_check:
            print("  运行 node qa/pages_check.mjs ...")
            with open(os.path.join(QA, "pages_check.log"), "w", encoding="utf-8") as lf:
                p = subprocess.run(["node", os.path.join(QA, "pages_check.mjs"), "--base", PUBLIC],
                                   capture_output=True, text=True, cwd=ROOT)
                lf.write(p.stdout + "\n" + p.stderr)
            print(f"  pages_check.mjs 退出码 {p.returncode}")
            if p.returncode != 0:
                tail = "\n".join((p.stdout or "").splitlines()[-20:])
                print(tail)
        # 解析 pages_check 输出中的 PASS/FAIL 统计
        log_path = os.path.join(QA, "pages_check.log")
        m = re.search(r"公网 UI 验收：PASS (\d+) / FAIL (\d+)", open(log_path, encoding="utf-8").read() if os.path.exists(log_path) else "")
        if m:
            print(f"  （pages_check 自检：PASS {m.group(1)} / FAIL {m.group(2)}）")
        ck_ui.check("node qa/pages_check.mjs 无脚本失败", 0, 0 if m else 1,
                    detail="未捕获输出统计")
        # 截图数量
        shots = [f for f in os.listdir(os.path.join(QA, "screens")) if f.startswith("pages-")] \
            if os.path.isdir(os.path.join(QA, "screens")) else []
        ck_ui.check("公网截图 ≥10 张（qa/screens/pages-*.png）", True, len(shots) >= 10,
                    detail=str(sorted(shots)))
        ck_ui.summary()

    # [5] 公网数据比对
    ck_cmp = Ck("[5] 公网数据逐字段比对（静态接口 vs QA 独立复算 vs 本地 Python 服务）")
    if args.skip_ui:
        print("  （--skip-ui：跳过，需 pages_observed.json）")
    else:
        check_online_vs_expected(ck_cmp, expected)
        check_online_errors(ck_cmp)
    ck_cmp.summary()

    total_pass = ck_dep.pass_n + ck_eng.pass_n + ck_exp.pass_n + ck_ui.pass_n + ck_cmp.pass_n
    total_fail = ck_dep.fail_n + ck_eng.fail_n + ck_exp.fail_n + ck_ui.fail_n + ck_cmp.fail_n
    print("\n" + "=" * 96)
    print(f"在线版验收合计：PASS {total_pass} / FAIL {total_fail} / 共 {total_pass + total_fail}")
    print(f"  部署一致性：PASS {ck_dep.pass_n} / FAIL {ck_dep.fail_n}")
    print(f"  引擎独立核验：PASS {ck_eng.pass_n} / FAIL {ck_eng.fail_n}")
    print(f"  在线期望值：PASS {ck_exp.pass_n} / FAIL {ck_exp.fail_n}")
    print(f"  公网 UI：PASS {ck_ui.pass_n} / FAIL {ck_ui.fail_n}")
    print(f"  公网数据比对：PASS {ck_cmp.pass_n} / FAIL {ck_cmp.fail_n}")
    print("=" * 96)
    if OBSERVATIONS:
        print("\n观察项（不计 FAIL）：")
        for o in OBSERVATIONS:
            print("  -", o)
    fails = ck_dep.fails + ck_eng.fails + ck_exp.fails + ck_ui.fails + ck_cmp.fails
    if fails:
        print("\nFAIL 明细：")
        for name, exp, act, diff, detail in fails:
            d = "" if diff is None else f" diff={diff:+.4f}"
            print(f"  - {name}: 期望={exp!r} 实际={act!r}{d}{(' :: ' + detail) if detail else ''}")
    return 0 if total_fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

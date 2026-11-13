"""异动雷达 · HTTP 服务（标准库实现，无第三方依赖）。

用法:
  python3 app.py                 # 启动服务，默认 http://127.0.0.1:8765
  python3 app.py --open          # 启动并打开浏览器
  python3 app.py --port 9000     # 指定端口
  python3 app.py --selftest      # 运行历史快照自检（离线，基于 core/fixtures）
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import sys
import threading
import time
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_ROOT)

from core import data_source, engine  # noqa: E402

WEB_DIR = os.path.join(PROJECT_ROOT, "web")
MD = data_source.MarketData()

MIME = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".mjs": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
    ".txt": "text/plain; charset=utf-8",
    ".map": "application/json; charset=utf-8",
}

DISCLAIMER = (
    "本工具按公开交易规则辅助估算，不构成投资建议；是否构成异常波动 / 严重异常波动，"
    "以交易所认定及上市公司公告为准。"
)
DATA_NOTE = "以下严重异动监管触发线，会根据指数进行变化"
SOURCE_LABEL = {"eastmoney": "东方财富", "10jqka": "同花顺", "cache": "本地缓存"}


def _source_label(raw: str) -> str:
    names = []
    for key in (raw or "").split("+"):
        if key in SOURCE_LABEL:
            names.append(SOURCE_LABEL[key])
        elif key.startswith("cache"):
            names.append("本地缓存")
    return " / ".join(dict.fromkeys(names)) or "公开行情"


def _pct_change(value: float, base: float) -> float:
    if not base:
        return 0.0
    return engine.round_half_up((value / base - 1.0) * 100, 2)


def build_trigger_payload(code: str, at: str | None = None, price: str | None = None, index_price: str | None = None) -> dict:
    replay_at = None
    if at:
        try:
            replay_at = _dt.datetime.fromisoformat(at)
        except ValueError:
            raise data_source.DataError("BAD_CODE", "at 参数格式应为 ISO8601 时间", 400)
    try:
        price_f = float(price) if price not in (None, "") else None
        index_f = float(index_price) if index_price not in (None, "") else None
    except ValueError:
        raise data_source.DataError("BAD_CODE", "price/index_price 必须是数字", 400)
    ctx = data_source.build_context(
        code, MD, at=replay_at, price_override=price_f, index_override=index_f
    )
    result = engine.compute(
        series=ctx["series"],
        price=ctx["price"],
        index_price=ctx["index_price"],
        prev_close=ctx["prev_close"],
        show_t0=ctx["show_t0"],
        future_dates=ctx["future_dates"],
        board=ctx["board"],
        market=ctx["market"],
        rows_count=10,
    )

    series = ctx["series"]
    prev_bar = series[-2] if len(series) >= 2 else series[-1]
    stock_pct = _pct_change(ctx["price"], prev_bar["sc"])
    index_pct = _pct_change(ctx["index_price"], prev_bar["ic"])

    if ctx["suspended"]:
        status = "suspended"
    elif ctx["phase"] == "pre_open":
        status = "pre_open"
    elif ctx["phase"] in ("trading", "lunch"):
        status = "trading"
    else:
        status = "closed"

    meta = {
        "index_label": ctx["index_quote"]["name"],
        "row_horizon": 10,
        "data_source": "东方财富 / 同花顺",
        "data_note": DATA_NOTE,
        "disclaimer": DISCLAIMER,
    }
    if ctx.get("calendar_warning"):
        meta["calendar_warning"] = ctx["calendar_warning"]

    return {
        "stock": {
            "code": ctx["code"],
            "name": ctx["name"],
            "market": ctx["market"],
            "board": ctx["board"],
            "board_label": ctx["board_label"],
            "secid": ctx["secid"],
            "price": engine.round_half_up(ctx["price"], 2),
            "prev_close": engine.round_half_up(series[-2]["sc"], 2) if len(series) >= 2 else None,
            "pct": stock_pct,
            "open": ctx["stock_quote"].get("open"),
            "high": ctx["stock_quote"].get("high"),
            "low": ctx["stock_quote"].get("low"),
            "status": status,
            "suspended": bool(ctx["suspended"]),
        },
        "index": {
            "code": ctx["index_quote"]["code"],
            "name": ctx["index_quote"]["name"],
            "price": engine.round_half_up(ctx["index_price"], 2),
            "prev_close": engine.round_half_up(prev_bar["ic"], 2),
            "pct": index_pct,
        },
        "snapshot": {
            "trade_date": engine.iso(ctx["hist_end"]),
            "time": ctx["now"].strftime("%H:%M:%S"),
            "phase": ctx["phase"],
            "is_trading_day": bool(ctx["is_trading_day"]),
            "show_t0": bool(ctx["show_t0"]),
            "updated_at": ctx["now"].isoformat(timespec="seconds"),
        },
        "risk": result["risk"],
        "summary": result["summary"],
        "reset": result["reset"],
        "rows": result["rows"],
        "meta": meta,
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "YidongRadar/1.0"
    protocol_version = "HTTP/1.1"

    # ---- 基础响应 ----
    def _send(self, status: int, body: bytes, content_type: str, cache: str = "no-store"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _send_json(self, status: int, payload: dict):
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _ok(self, data: dict):
        self._send_json(200, {"ok": True, "data": data})

    def _err(self, code: str, message: str, status: int = 500):
        self._send_json(status, {"ok": False, "error": {"code": code, "message": message}})

    # ---- 日志（只打印 API） ----
    def log_message(self, fmt, *args):  # noqa: N802
        if self.path.startswith("/api/"):
            sys.stderr.write(f"[{time.strftime('%H:%M:%S')}] {self.address_string()} {fmt % args}\n")

    # ---- 静态文件 ----
    def _serve_static(self, path: str):
        rel = path.lstrip("/") or "index.html"
        if rel.endswith("/"):
            rel += "index.html"
        target = os.path.normpath(os.path.join(WEB_DIR, rel))
        if not target.startswith(WEB_DIR + os.sep) and target != WEB_DIR:
            self._err("FORBIDDEN", "非法路径", 403)
            return
        if os.path.isdir(target):
            target = os.path.join(target, "index.html")
        if not os.path.isfile(target):
            if rel in ("favicon.ico", "robots.txt"):
                self._send(204, b"", "text/plain; charset=utf-8", cache="public, max-age=86400")
                return
            # 单页应用回退
            target = os.path.join(WEB_DIR, "index.html")
            if not os.path.isfile(target):
                self._err("NOT_FOUND", "前端页面尚未就绪（web/index.html 不存在）", 404)
                return
        ext = os.path.splitext(target)[1].lower()
        with open(target, "rb") as f:
            body = f.read()
        self._send(200, body, MIME.get(ext, "application/octet-stream"), cache="no-cache")

    # ---- 路由 ----
    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)
        try:
            if path == "/api/health":
                self._ok(
                    {
                        "status": "ok",
                        "time": data_source.now_shanghai().isoformat(timespec="seconds"),
                        "version": "1.0.0",
                    }
                )
                return
            if path == "/api/search":
                keyword = (qs.get("q") or [""])[0].strip()
                if not keyword:
                    self._err("BAD_CODE", "请输入股票代码、名称或拼音首字母", 400)
                    return
                items = MD.search(keyword)
                self._ok({"items": items})
                return
            if path == "/api/triggers":
                code = (qs.get("code") or [""])[0].strip()
                if not code:
                    self._err("BAD_CODE", "缺少 code 参数", 400)
                    return
                at = (qs.get("at") or [None])[0]
                price = (qs.get("price") or [None])[0]
                index_price = (qs.get("index_price") or [None])[0]
                self._ok(build_trigger_payload(code, at, price, index_price))
                return
            if path.startswith("/api/"):
                self._err("NOT_FOUND", "接口不存在", 404)
                return
            self._serve_static(path)
        except data_source.DataError as exc:
            self._err(exc.code, exc.message, exc.http_status)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            self._err("INTERNAL", "服务内部错误，请稍后重试", 500)


def main():
    parser = argparse.ArgumentParser(description="异动雷达 · 严重异动监管触发线")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址（默认 127.0.0.1）")
    parser.add_argument("--port", type=int, default=8765, help="监听端口（默认 8765）")
    parser.add_argument("--open", action="store_true", help="启动后自动打开浏览器")
    parser.add_argument("--selftest", action="store_true", help="运行历史快照自检后退出")
    args = parser.parse_args()

    if args.selftest:
        from core.selftest import run_selftest

        ok = run_selftest()
        sys.exit(0 if ok else 1)

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    httpd.daemon_threads = True

    url = f"http://{args.host}:{args.port}/"
    print("")
    print("  异动雷达 · 严重异动监管触发线")
    print(f"  已启动:  {url}")
    print("  数据源:  东方财富（主）/ 同花顺（备），公开行情，仅供研究参考")
    print("  自检:    python3 app.py --selftest")
    print("  停止:    Ctrl+C")
    print("")

    if args.open:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()

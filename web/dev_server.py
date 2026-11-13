#!/usr/bin/env python3
"""异动雷达 · 本地开发预览服务器（仅开发用，生产环境不需要）。

用途
----
在没有任何后端的情况下预览前端，并把 ``docs/api-sample-*.json`` 包装成
``/api/triggers`` 与 ``/api/search`` 的响应，方便前端自测。

用法
----
    python3 web/dev_server.py                 # http://127.0.0.1:8099/
    PORT=8101 python3 web/dev_server.py       # 换端口
    YDRADAR_SCENARIO=closed python3 web/dev_server.py      # 模拟收盘后
    YDRADAR_SCENARIO=suspended python3 web/dev_server.py   # 模拟停牌

生产环境：真实后端以 ``web/`` 为静态根目录，直接提供 ``/api/*``，不使用本文件。
"""

from __future__ import annotations

import copy
import json
import os
import sys
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

WEB_DIR = Path(__file__).resolve().parent
DOCS_DIR = WEB_DIR.parent / "docs"

SAMPLE_TRIGGERS = json.loads((DOCS_DIR / "api-sample-triggers.json").read_text(encoding="utf-8"))
SAMPLE_SEARCH = json.loads((DOCS_DIR / "api-sample-search.json").read_text(encoding="utf-8"))

# 开发期把另外两只示例股也映射到同一份示范数据，便于点开示例 chip 看页面。
MOCK_IDENTITIES = {
    "600664": {"code": "600664", "name": "哈药股份", "market": "sh", "board": "main",
               "board_label": "沪主板", "secid": "1.600664"},
    "300750": {"code": "300750", "name": "宁德时代", "market": "sz", "board": "gem",
               "board_label": "创业板", "secid": "0.300750"},
}

PINYIN = {"mddl": "000993", "hygf": "600664", "ndsd": "300750"}

PORT = int(os.environ.get("PORT", "8099"))
SCENARIO = os.environ.get("YDRADAR_SCENARIO", "").strip().lower()


def json_bytes(payload: dict) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def make_triggers_payload(code: str):
    """返回 (http_status, payload)。code 不存在时给出 404 NOT_FOUND。"""
    if code == "999999":
        return 400, {"ok": False, "error": {"code": "NOT_SUPPORTED", "message": "B 股暂不支持"}}
    if code not in ("000993",) and code not in MOCK_IDENTITIES:
        return 404, {"ok": False, "error": {"code": "NOT_FOUND", "message": "未找到该股票，请检查代码是否正确"}}

    data = copy.deepcopy(SAMPLE_TRIGGERS["data"])
    if code in MOCK_IDENTITIES:
        data["stock"].update(MOCK_IDENTITIES[code])

    if SCENARIO == "closed":
        snap = data["snapshot"]
        snap["phase"] = "closed"
        snap["show_t0"] = False
        snap["is_trading_day"] = True
        # 收盘态：示例行右移为 T+1..T+10（仅用于本地视觉检查，真实逻辑由后端提供）。
        rows = data["rows"]
        shifted = []
        for index, row in enumerate(rows):
            item = copy.deepcopy(row)
            item["key"] = f"T+{index + 1}"
            item["is_today"] = False
            shifted.append(item)
        last = copy.deepcopy(shifted[-1])
        last["key"] = f"T+{len(shifted) + 1}"
        shifted.append(last)
        data["rows"] = shifted
    elif SCENARIO == "suspended":
        data["stock"]["status"] = "suspended"
        data["stock"]["suspended"] = True

    return 200, {"ok": True, "data": data}


def make_search_payload(query: str):
    raw = (query or "").strip()
    if not raw:
        return 400, {"ok": False, "error": {"code": "BAD_CODE", "message": "q 不能为空"}}
    q = raw.lower()
    items = SAMPLE_SEARCH["data"]["items"]
    matched = [
        item for item in items
        if q in item["code"].lower()
        or q in item["name"].lower()
        or PINYIN.get(q) == item["code"]
        or q in "".join(item["name"])  # 名称逐字包含
    ]
    return 200, {"ok": True, "data": {"items": matched[:8]}}


class Handler(SimpleHTTPRequestHandler):
    server_version = "YidongRadarDev/0.1"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(WEB_DIR), **kwargs)

    def do_GET(self):  # noqa: N802 (标准库命名)
        parsed = urlparse(self.path)
        if parsed.path == "/api/health":
            self.send_json(200, {"ok": True, "data": {"status": "ok", "version": "dev-mock"}})
            return
        if parsed.path == "/api/triggers":
            params = parse_qs(parsed.query)
            code = (params.get("code") or [""])[0].strip()
            if not (len(code) == 6 and code.isdigit()):
                self.send_json(400, {"ok": False, "error": {"code": "BAD_CODE", "message": "code 应为 6 位数字"}})
                return
            status, payload = make_triggers_payload(code)
            self.send_json(status, payload)
            return
        if parsed.path == "/api/search":
            params = parse_qs(parsed.query)
            q = (params.get("q") or [""])[0]
            status, payload = make_search_payload(q)
            self.send_json(status, payload)
            return
        super().do_GET()

    def send_json(self, status: int, payload: dict):
        body = json_bytes(payload)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def end_headers(self):
        # 开发预览禁用缓存，改完文件刷新即可见。
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        super().end_headers()

    def log_message(self, fmt, *args):  # 精简访问日志
        sys.stderr.write("[dev] %s - %s\n" % (self.address_string(), fmt % args))


def main():
    if not (WEB_DIR / "index.html").exists():
        raise SystemExit("web/index.html 不存在")
    if SCENARIO:
        sys.stderr.write("[dev] YDRADAR_SCENARIO=%s（仅本地 mock）\n" % SCENARIO)
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    server.allow_reuse_address = True
    sys.stderr.write("[dev] 异动雷达 dev server：http://127.0.0.1:%d/\n" % PORT)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()

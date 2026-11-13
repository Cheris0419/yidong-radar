#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
异动雷达 · QA 专用 Mock 服务（仅用于前端状态验收，不修改 web/ 与后端代码）
=========================================================================
把真实的 web/ 静态文件 + 受控的 /api/* JSON 组合起来，用于覆盖真实行情里难以
同时出现的 UI 状态：触发涨幅为负（已超线）、停牌。

用法：python3 qa/mock_server.py [--port 8799] [--web ../web] [--fixtures ./fixtures]
"""
from __future__ import annotations

import argparse
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse

ROOT = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.abspath(os.path.join(ROOT, "..", "web"))
FIXTURES = os.path.join(ROOT, "fixtures")
MIME = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
        ".js": "text/javascript; charset=utf-8", ".svg": "image/svg+xml",
        ".json": "application/json; charset=utf-8", ".png": "image/png"}


class Handler(BaseHTTPRequestHandler):
    server_version = "YidongRadarQA/1.0"

    def log_message(self, fmt, *args):  # noqa: A003 - 静默
        pass

    # ---------- 工具 ----------
    def _json(self, obj, status: int = 200) -> None:
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def _file(self, path: str) -> None:
        if not os.path.isfile(path):
            self._json({"ok": False, "error": {"code": "NOT_FOUND",
                                               "message": "static not found"}}, 404)
            return
        ext = os.path.splitext(path)[1].lower()
        raw = open(path, "rb").read()
        self.send_response(200)
        self.send_header("Content-Type", MIME.get(ext, "application/octet-stream"))
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    # ---------- 路由 ----------
    def do_GET(self):  # noqa: N802
        u = urlparse(self.path)
        p = u.path
        qs = {}
        for part in u.query.split("&"):
            if "=" in part:
                k, v = part.split("=", 1)
                qs[k] = unquote(v)
        if p == "/api/health":
            self._json({"ok": True, "data": {"status": "ok",
                                             "time": "2026-11-12T14:30:00+08:00",
                                             "version": "mock"}})
        elif p == "/api/search":
            fixture = os.path.join(FIXTURES, "search_000993.json")
            self._json(json.load(open(fixture, encoding="utf-8")))
        elif p == "/api/triggers":
            code = qs.get("code", "")
            name = {"000993": "triggers_negative.json",
                    "600664": "triggers_suspended.json"}.get(code,
                                                             "triggers_000993_live.json")
            self._json(json.load(open(os.path.join(FIXTURES, name), encoding="utf-8")))
        elif p in ("/", "/index.html"):
            self._file(os.path.join(WEB, "index.html"))
        else:
            safe = os.path.normpath(p.lstrip("/"))
            if safe.startswith(".."):
                return self._json({"ok": False}, 400)
            self._file(os.path.join(WEB, safe))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8799)
    args = ap.parse_args()
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"QA mock server: http://127.0.0.1:{args.port}/  (web={WEB})", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

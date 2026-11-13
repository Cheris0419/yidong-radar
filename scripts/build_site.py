#!/usr/bin/env python3
"""把 web/ 前端打包为纯静态在线版 site/（GitHub Pages 用）。

- 复制 web/styles.css、web/app.js、web/favicon.svg 到 site/
- 由 web/index.html 生成 site/index.html，并在 app.js 之前注入静态 API 适配脚本
- site/calendar.js、site/engine.js、site/data.js、site/static-api.js 为静态版专属文件，本脚本不覆盖

用法：python3 scripts/build_site.py
"""
from __future__ import annotations

import os
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB = os.path.join(ROOT, "web")
SITE = os.path.join(ROOT, "site")

STATIC_SCRIPTS = [
    "calendar.js",
    "engine.js",
    "data.js",
    "static-api.js",
]
COPY_FILES = ["styles.css", "app.js", "favicon.svg"]


def main() -> int:
    os.makedirs(SITE, exist_ok=True)

    for name in COPY_FILES:
        shutil.copyfile(os.path.join(WEB, name), os.path.join(SITE, name))

    with open(os.path.join(WEB, "index.html"), encoding="utf-8") as f:
        html = f.read()

    injection = "\n".join(f'<script src="{name}" defer></script>' for name in STATIC_SCRIPTS)
    html = html.replace('<script src="app.js" defer></script>', injection + '\n<script src="app.js" defer></script>')
    html = html.replace(
        "<title>异动雷达 · 严重异动监管触发线</title>",
        "<title>异动雷达 · 严重异动监管触发线（在线版）</title>",
    )
    html = html.replace(
        '<meta name="description" content="输入 A 股代码，查看未来 10 个交易日的严重异动监管触发价与触发涨幅。">',
        '<meta name="description" content="在线版：浏览器直连公开行情，测算 A 股严重异动监管触发价与触发涨幅（10日100% / 30日200% / 十日同向异动线）。">',
    )
    with open(os.path.join(SITE, "index.html"), "w", encoding="utf-8") as f:
        f.write(html)

    print("site/ 已生成：")
    for name in sorted(os.listdir(SITE)):
        print("  -", name)
    return 0


if __name__ == "__main__":
    sys.exit(main())

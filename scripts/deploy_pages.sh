#!/usr/bin/env bash
# 把 site/ 静态在线版发布到 GitHub Pages（gh-pages 分支）
#
# 首次使用（仓库已存在、Pages 未启用）：
#   1) 先启用 Pages：仓库 Settings → Pages → Source 选 "Deploy from a branch" → 分支 gh-pages / 根目录
#   2) 运行本脚本推送静态站
#
# 日常更新：改完 web/ 或 site/ 后运行
#   ./scripts/deploy_pages.sh
set -euo pipefail
cd "$(dirname "$0")/.."

REMOTE="${REMOTE:-origin}"

echo "==> 重新生成 site/（同步 web/ 前端）"
python3 scripts/build_site.py

echo "==> 离线自检静态引擎"
node scripts/test_site_engine.js

echo "==> 推送 site/ 到 ${REMOTE} gh-pages"
TREE=$(git subtree split --prefix site HEAD)
git push "${REMOTE}" "${TREE}:refs/heads/gh-pages" --force

echo "==> 完成。GitHub Pages 地址：https://<owner>.github.io/<repo>/"

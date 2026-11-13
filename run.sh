#!/usr/bin/env bash
# 异动雷达 · 一键启动脚本
# 用法：./run.sh          （默认 8765 端口，自动打开浏览器）
#      PORT=9000 ./run.sh （自定义端口）
set -euo pipefail
cd "$(dirname "$0")"

PORT="${PORT:-8765}"

if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "端口 $PORT 已被占用。请先停止旧进程，或使用：PORT=其他端口 ./run.sh"
  exit 1
fi

exec python3 app.py --port "$PORT" --open

# 异动雷达 · 本地服务版（Python 标准库，无第三方依赖）
FROM python:3.11-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1

COPY app.py run.sh ./
COPY core/ ./core/
COPY web/ ./web/
COPY docs/METHODOLOGY.md ./docs/METHODOLOGY.md

EXPOSE 8765
# 云平台通常注入 $PORT；默认 8765
CMD ["sh", "-c", "python3 app.py --host 0.0.0.0 --port ${PORT:-8765}"]

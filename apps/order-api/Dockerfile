FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

EXPOSE 8000

# `opentelemetry-instrument` が SDK 初期化と自動計装をまとめて行う。
# 接続先やサービス名は環境変数 (OTEL_*) で外から与える = 12factor 的で運用しやすい。
CMD ["opentelemetry-instrument", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

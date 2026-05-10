# syntax=docker/dockerfile:1.7
# ---------- Builder: 依存関係を uv でインストール ----------
FROM python:3.13-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv

# uv をコピー（公式イメージ提供のバイナリ）
COPY --from=ghcr.io/astral-sh/uv:0.5.4 /uv /usr/local/bin/uv

WORKDIR /app

# 依存関係解決のため、まずロックファイル類だけコピー → キャッシュ効率化
COPY pyproject.toml uv.lock* ./

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project || \
    uv sync --no-dev --no-install-project

# ---------- Runtime: 最小ランタイム ----------
FROM python:3.13-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/opt/venv/bin:$PATH

# 非 root ユーザーで動作させる
RUN groupadd --system app && useradd --system --gid app --home /app app

WORKDIR /app

# 依存関係を builder から、アプリケーションコードはホストからコピー
COPY --from=builder /opt/venv /opt/venv
COPY app ./app

USER app
EXPOSE 8000

# ECS の awslogs ドライバへ stdout を流す前提
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

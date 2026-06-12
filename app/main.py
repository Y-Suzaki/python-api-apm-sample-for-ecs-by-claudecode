"""FastAPI アプリケーションのエントリポイント。

ローカル実行例:
    uv run uvicorn app.main:app --reload

ECS Fargate 上では Dockerfile の CMD から uvicorn が起動する。
"""
from fastapi import FastAPI

from app.api.configuration import router as configuration_router
from app.api.users import router as users_router
from app.core.config import get_settings
from app.core.telemetry import setup_tracing

settings = get_settings()

app = FastAPI(
    title=settings.app_name,
    version="0.1.0",
    description="ユーザー管理サンプル API（FastAPI + DynamoDB on ECS Fargate）",
)


@app.get("/health", tags=["meta"], summary="ALB ヘルスチェック")
def health() -> dict[str, str]:
    """ALB ターゲットグループのヘルスチェック用エンドポイント。"""
    return {"status": "ok", "env": settings.app_env}


app.include_router(users_router)
app.include_router(configuration_router)

# OpenTelemetry → CloudWatch Agent（サイドカー）→ AWS Application Signals の計装は
# ルーター登録後にまとめて行う（FastAPIInstrumentor が既存ルートを拾うため）。
setup_tracing(app, service_name=settings.app_name, environment=settings.app_env)

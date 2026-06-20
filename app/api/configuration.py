"""トレース確認用の ``/configuration`` エンドポイント。

外部 HTTP API（ipify）を呼んで結果を返すだけの薄いエンドポイント。機能としての
意味は薄いが、``opentelemetry-instrumentation-httpx`` による自動計装で外部
HTTP 呼び出しがクライアントスパン化され、X-Ray のサービスマップに外部 HTTP
ノードが現れることを確認するために用意している。

ECS タスクは Private Subnet に居るので、ここで返ってくる ``outbound_ip`` は
NAT Gateway に紐付いている Elastic IP になる（ALB→ECS→NAT→Internet という経路の
副次的な確認にもなる）。
"""
from __future__ import annotations

import httpx
import logging
import time
from fastapi import APIRouter, HTTPException, status
from opentelemetry import trace
from pydantic import BaseModel, Field

from app.api.deps import SettingsDep

router = APIRouter(tags=["configuration"])
logger = logging.getLogger(__name__)
# 自動計装ではカバーされない独自処理（calc_configuration）を手動でスパン化する
# ための tracer。``__name__`` を渡しておくと X-Ray 上で発行元モジュールが分かる。
tracer = trace.get_tracer(__name__)


# 無料・無認証・JSON で公開グローバル IP を返す軽量サービス。
_IPIFY_URL = "https://api.ipify.org"
# 外部 API 呼び出しのタイムアウト。NAT Gateway 経由で多少遅くなる可能性を考慮。
_HTTPX_TIMEOUT = httpx.Timeout(connect=3.0, read=5.0, write=5.0, pool=5.0)


class Configuration(BaseModel):
    """``/configuration`` のレスポンスモデル。"""

    service_name: str = Field(..., description="このサービスの ``app_name`` 設定値")
    environment: str = Field(..., description="``APP_ENV``（local / prod など）")
    outbound_ip: str = Field(
        ...,
        description="ipify から取得したグローバル送信元 IP（= NAT Gateway の EIP）",
    )


def calc_configuration():
    """ ダミー処理を行う関数。トレーシング計測用のため実装内容に意味はない。

    呼び出しを X-Ray のサブセグメントとして可視化するため、関数本体を
    ``tracer.start_as_current_span`` で包んで手動スパンを 1 本発行する。
    現在のリクエストスパン（FastAPIInstrumentor が生成する SERVER スパン）が
    親になるので、X-Ray のトレース詳細では ``/configuration`` の子として表示される。
    """
    with tracer.start_as_current_span("calc_configuration"):
        logger.info("Call function calc_configuration.")
        time.sleep(0.5)


@router.get(
    "/configuration",
    response_model=Configuration,
    summary="サンプル設定取得（外部 HTTP 呼び出しのトレース確認用）",
)
async def get_configuration(settings: SettingsDep) -> Configuration:
    """外部 API（ipify）を 1 回叩き、自サービスの設定値とあわせて返す。"""
    try:
        async with httpx.AsyncClient(timeout=_HTTPX_TIMEOUT) as client:
            resp = await client.get(_IPIFY_URL, params={"format": "json"})
            resp.raise_for_status()
    except httpx.HTTPError as exc:
        # 外部 API が落ちている／到達不能の場合は 502 で表面化する。
        # この経路でも httpx 計装スパンは生成されるので、X-Ray 側でエラーが見える。
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"failed to call external API ({_IPIFY_URL}): {exc}",
        ) from exc

    body = resp.json()

    # ipify 応答取得後、独自処理（手動スパン化済み）を 1 回挟む。
    # X-Ray サービスマップ／トレース詳細で外部 HTTP スパンと並んで表示される想定。
    calc_configuration()

    return Configuration(
        service_name=settings.app_name,
        environment=settings.app_env,
        outbound_ip=body.get("ip", "unknown"),
    )

"""OpenTelemetry トレーシングのセットアップ（AWS X-Ray 互換）。

設計方針
========
* ADOT Collector（ECS タスク内のサイドカー）に対して OTLP/HTTP でスパンを送信する。
  サイドカー前提なのでエンドポイントは ``http://localhost:4318``。
* AWS X-Ray にそのまま流し込むため、トレース ID 生成器は X-Ray 仕様
  （``1-{8桁epoch}-{96bitランダム}``）に切り替える。
* ALB が付与する ``X-Amzn-Trace-Id`` ヘッダから親コンテキストを継承するため、
  グローバルプロパゲーターも X-Ray 形式にする。これで ALB→ECS→DynamoDB が
  単一トレース ID でつながる。
* 自動計装は FastAPI（受信側）と botocore（DynamoDB 呼び出し）の二つ。
* ``OTEL_EXPORTER_OTLP_ENDPOINT`` が未設定のローカル開発時はエクスポーターを
  接続せず、計装だけ有効にしておく（X-Ray に送らないだけで API は壊れない）。
"""
from __future__ import annotations

import logging
import os
from collections.abc import Iterable

from fastapi import FastAPI
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.botocore import BotocoreInstrumentor
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.propagate import set_global_textmap
from opentelemetry.propagators.aws import AwsXRayPropagator
from opentelemetry.sdk.extension.aws.trace import AwsXRayIdGenerator
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

logger = logging.getLogger(__name__)

# OTLP/HTTP のトレース送信先パス（Collector 既定）。
_OTLP_TRACES_PATH = "/v1/traces"

# ALB ヘルスチェックのように常時叩かれて成功するエンドポイントは既定で間引く。
# 失敗（5xx・例外）時のスパンは引き続き X-Ray に送られる。
_DEFAULT_NOISE_PATHS: tuple[str, ...] = ("/health",)


class _NoiseSpanFilterProcessor(SpanProcessor):
    """エクスポート前に 2 種類の「ノイズ」スパンを捨てる委譲ラッパー。

    1. **ASGI 内部イベントスパン**（``asgi.event.type`` 属性を持つ
       ``... http send`` / ``... http receive``）。
       FastAPIInstrumentor 配下の ``opentelemetry-instrumentation-asgi`` が
       1 リクエストあたり 2〜3 個自動生成する子スパンで、ASGI の
       ``http.response.start`` / ``http.response.body`` / ``http.request`` を
       それぞれスパン化したもの。X-Ray のサブセグメントとして冗長に並ぶため、
       TTFB／ボディフラッシュの厳密な計測が不要なら捨てて差し支えない。

    2. **指定パスへの成功 SERVER スパン**（既定では ``/health``）。
       失敗時（``span.status == ERROR`` または 5xx）は通常通り送るため、
       ヘルスチェックが落ちた瞬間の可観測性は維持される。

    内部で ``BatchSpanProcessor`` を委譲ラップし、``on_end`` の時点で属性を見て
    判定する。エクスポート前の間引きなので OTLP 送信量・X-Ray のトレース一覧の
    両方が軽くなる。

    なお ``/health`` は子スパン（DynamoDB 呼び出し等）を作らないので、親スパンを
    捨てても孤児スパンは発生しない。子スパンを持つパスを対象にする場合は、
    Collector 側の ``tail_sampling`` プロセッサ等を検討すること。
    """

    def __init__(self, inner: SpanProcessor, paths: Iterable[str]) -> None:
        self._inner = inner
        self._paths = frozenset(paths)

    def on_start(self, span, parent_context=None) -> None:
        # 開始時点では成否が確定しないので無条件で委譲する。
        self._inner.on_start(span, parent_context)

    def on_end(self, span: ReadableSpan) -> None:
        if self._should_drop(span):
            return
        self._inner.on_end(span)

    def shutdown(self) -> None:
        self._inner.shutdown()

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        return self._inner.force_flush(timeout_millis)

    def _should_drop(self, span: ReadableSpan) -> bool:
        attrs = span.attributes or {}

        # (1) ASGI 内部イベント（http send / http receive）は無条件で捨てる。
        #     FastAPIInstrumentor が透過的に生成するため、コード側で発生を抑止できない。
        if "asgi.event.type" in attrs:
            return True

        # (2) 以降はサーバ側の HTTP スパンに対する成功間引きのみ。
        if span.kind != trace.SpanKind.SERVER:
            return False

        # OpenTelemetry HTTP セマンティック規約は新旧 2 系統あるため両方を見る。
        path = (
            attrs.get("http.route")
            or attrs.get("url.path")
            or attrs.get("http.target")
        )
        if path not in self._paths:
            return False

        # 例外発生で span status が ERROR になっているケースは残す。
        if span.status.status_code == trace.StatusCode.ERROR:
            return False

        # 5xx（明示的に内部エラーレスポンスを返した場合）も残す。
        status_code = (
            attrs.get("http.response.status_code")
            or attrs.get("http.status_code")
        )
        if isinstance(status_code, int) and status_code >= 500:
            return False

        return True


def setup_tracing(
    app: FastAPI,
    service_name: str,
    environment: str,
    *,
    drop_successful_paths: Iterable[str] = _DEFAULT_NOISE_PATHS,
) -> None:
    """FastAPI アプリにトレーシングを組み込む。

    アプリ起動時に 1 回だけ呼ぶこと。多重に呼ぶと TracerProvider が再生成され、
    ``FastAPIInstrumentor`` が二重計装される可能性がある。

    ``drop_successful_paths`` で指定したパスへのリクエストは、成功時に限り
    エクスポート対象から外す（失敗時は通常通りトレースされる）。
    """
    # 1) ALB → アプリ間の親子関係を保つために、X-Ray ヘッダを認識する
    #    プロパゲーターをグローバルに差し込む。
    set_global_textmap(AwsXRayPropagator())

    # 2) X-Ray のサービスマップで識別される名前・環境タグ。
    resource = Resource.create(
        {
            "service.name": service_name,
            "deployment.environment": environment,
        }
    )

    # 3) X-Ray 互換のトレース ID 生成器を使う TracerProvider を構築。
    provider = TracerProvider(
        resource=resource,
        id_generator=AwsXRayIdGenerator(),
    )

    # 4) サイドカー Collector の OTLP/HTTP エンドポイントへバッチ送信。
    #    OTEL_EXPORTER_OTLP_ENDPOINT が未設定（=ローカル開発）の場合はエクスポーター
    #    を取り付けず、自動計装だけ有効にする。
    #    ヘルスチェック等の成功スパンは BatchSpanProcessor の前段で間引く。
    base_endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
    if base_endpoint:
        traces_endpoint = base_endpoint.rstrip("/") + _OTLP_TRACES_PATH
        exporter = OTLPSpanExporter(endpoint=traces_endpoint)
        batch_processor = BatchSpanProcessor(exporter)
        provider.add_span_processor(
            _NoiseSpanFilterProcessor(
                batch_processor,
                paths=drop_successful_paths,
            )
        )
        logger.info(
            "OTLP span exporter enabled: %s "
            "(drop ASGI internal spans + successful spans for paths=%s)",
            traces_endpoint,
            sorted(drop_successful_paths),
        )
    else:
        logger.info("OTEL_EXPORTER_OTLP_ENDPOINT not set; spans will not be exported.")

    trace.set_tracer_provider(provider)

    # 5) 自動計装：
    #    - FastAPI: 受信リクエスト／レスポンス
    #    - botocore: boto3 が呼ぶ AWS API（DynamoDB など）
    #    - httpx: 外部 HTTP API 呼び出し（/configuration から ipify を叩く等）
    FastAPIInstrumentor.instrument_app(app)
    BotocoreInstrumentor().instrument()
    HTTPXClientInstrumentor().instrument()

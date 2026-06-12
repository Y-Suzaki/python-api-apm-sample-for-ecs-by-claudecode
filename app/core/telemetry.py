"""OpenTelemetry トレーシングのセットアップ（AWS Application Signals 互換）。

設計方針
========
* CloudWatch Agent サイドカー（ECS タスク内）に対して OTLP/HTTP で
  トレース・メトリクスを送信し、AWS Application Signals で可視化する。
* AWS Application Signals 用の SpanProcessor / MeterProvider 構成は
  ``aws-opentelemetry-distro`` の ``AwsOpenTelemetryConfigurator`` に任せる。
  本来は ``opentelemetry-instrument`` ラッパー経由で起動した時に sitecustomize.py
  から自動発火する Configurator を、``setup_tracing()`` から明示的に呼び出す
  ことで、uvicorn 直接起動の構成のまま distro の恩恵を受ける。
* distro が組み立てるコンポーネント：
    - ``AwsXRayIdGenerator``（X-Ray 形式トレース ID）
    - ``AwsXRayPropagator``（``X-Amzn-Trace-Id`` の継承）
    - ``AwsSpanMetricsProcessor`` / ``AwsMetricAttributesSpanProcessor``
      （スパンから RED メトリクスを派生 + サービスマップ用属性付与）
    - ``BatchSpanProcessor`` + OTLP exporter（CW Agent への送信）
* ``OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`` が未設定のローカル開発時は
  Configurator を呼ばず、最小限の TracerProvider を自前で組む
  （外部に接続を試みない）。

ノイズ抑制の方針
================
* **``/health`` への ALB ヘルスチェック**：
  ``FastAPIInstrumentor.instrument_app(app, excluded_urls=...)`` で計装対象から
  外す。これにより X-Ray のトレースだけでなく Application Signals メトリクス
  からも除外され、リクエスト数・レイテンシ・エラー率にヘルスチェックが
  混入しない。失敗時もスパンが作られないが、ALB の ``UnHealthyHostCount``
  メトリクスで別途監視できる前提。
* **ASGI 内部イベントスパン**（``asgi.event.type`` を持つ ``http send`` /
  ``http receive``）：``opentelemetry-instrumentation-asgi`` が 1 リクエスト
  あたり 2〜3 個自動生成する子スパン。``excluded_urls`` では落とせないため、
  distro が登録した ``BatchSpanProcessor`` の ``span_exporter`` 属性を
  ``_NoiseFilteringSpanExporter`` でラップし、export 直前に drop する。
  ``BatchSpanProcessor`` 自体は触らないので、distro が動的サンプリング用に
  保持している ``BatchSpanProcessor`` 参照（X-Ray リモートサンプラー連携）
  と競合しない。
"""
from __future__ import annotations

import logging
import os
from collections.abc import Sequence

from fastapi import FastAPI
from opentelemetry import trace
from opentelemetry.instrumentation.botocore import BotocoreInstrumentor
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.sdk.extension.aws.trace import AwsXRayIdGenerator
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    SpanExporter,
    SpanExportResult,
)

logger = logging.getLogger(__name__)

# ALB ヘルスチェックのパス。FastAPIInstrumentor の excluded_urls は
# re.search(pattern, full_url) で判定される。full_url は
# "http://10.x.x.x:8000/health" 形式のため、"^/health$" では先頭アンカーが
# "http://" に阻まれてマッチしない。"/health$" で末尾のみ固定する。
# （"/healthcheck" は末尾が "/health" で終わらないため誤マッチしない）
_EXCLUDED_URLS = r"/health$"


class _NoiseFilteringSpanExporter(SpanExporter):
    """ASGI 内部イベントスパンを export 直前に drop する委譲ラッパー。

    FastAPIInstrumentor 配下の ``opentelemetry-instrumentation-asgi`` は、
    1 リクエストにつき ASGI イベント（``http.response.start`` /
    ``http.response.body`` / ``http.request``）をそれぞれスパン化した
    ``asgi.event.type`` 属性付きの子スパンを 2〜3 個自動生成する。
    X-Ray のサブセグメント表示が冗長になるため、エクスポート手前で drop する。

    distro が登録した ``BatchSpanProcessor`` の ``span_exporter`` 属性を
    本クラスで置き換える運用。``BatchSpanProcessor`` 自体は触らないので、
    distro が X-Ray リモートサンプラーの動的調整のために保持している
    ``BatchSpanProcessor`` 参照と競合しない。
    """

    def __init__(self, inner: SpanExporter) -> None:
        self._inner = inner

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        filtered = [s for s in spans if not self._should_drop(s)]
        if not filtered:
            return SpanExportResult.SUCCESS
        return self._inner.export(filtered)

    def shutdown(self) -> None:
        self._inner.shutdown()

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        return self._inner.force_flush(timeout_millis)

    @staticmethod
    def _should_drop(span: ReadableSpan) -> bool:
        attrs = span.attributes or {}
        return "asgi.event.type" in attrs


def setup_tracing(
    app: FastAPI,
    service_name: str,
    environment: str,
) -> None:
    """FastAPI アプリにトレーシングを組み込む。

    アプリ起動時に 1 回だけ呼ぶこと。多重に呼ぶと TracerProvider が再生成され、
    ``FastAPIInstrumentor`` が二重計装される可能性がある。
    """
    logger.warning("DEBUG[telemetry]: setup_tracing called")
    # OTLP エンドポイントが設定されている時のみ distro 経由で
    # Application Signals を立ち上げる。未設定（=ローカル開発）の場合は
    # 外部接続を試みず、自動計装だけ有効にする。
    otlp_endpoint = (
        os.getenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT")
        or os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
    )
    logger.warning("DEBUG[telemetry]: otlp_endpoint=%s", otlp_endpoint)

    if otlp_endpoint:
        # service.name / deployment.environment は CFn の OTEL_SERVICE_NAME /
        # OTEL_RESOURCE_ATTRIBUTES で渡される想定だが、渡し損ねた場合の
        # フォールバックとして環境変数を設定する（既に設定されていれば
        # 上書きしない）。
        os.environ.setdefault("OTEL_SERVICE_NAME", service_name)
        os.environ.setdefault(
            "OTEL_RESOURCE_ATTRIBUTES",
            f"deployment.environment={environment}",
        )

        # AWS distro の Distro + Configurator を、opentelemetry-instrument
        # 経由起動時と同じ順序で呼ぶ。Distro は環境変数のデフォルト値を仕込み、
        # Configurator が TracerProvider / MeterProvider を構築する。
        from amazon.opentelemetry.distro.aws_opentelemetry_configurator import (
            AwsOpenTelemetryConfigurator,
        )
        from amazon.opentelemetry.distro.aws_opentelemetry_distro import (
            AwsOpenTelemetryDistro,
        )

        # distro の自動計装から FastAPI/ASGI を外す。
        # excluded_urls を効かせるため、後段で instrument_app を手動呼び出しする。
        os.environ.setdefault("OTEL_PYTHON_DISABLED_INSTRUMENTATIONS", "fastapi,asgi")

        logger.warning("DEBUG[telemetry]: calling AwsOpenTelemetryDistro().configure()")
        AwsOpenTelemetryDistro().configure()
        logger.warning(
            "DEBUG[telemetry]: calling AwsOpenTelemetryConfigurator().configure()"
        )
        AwsOpenTelemetryConfigurator().configure()
        logger.warning("DEBUG[telemetry]: distro configure() completed")

        # distro が登録した BatchSpanProcessor の SpanExporter を NoiseFilter で
        # ラップして、ASGI 内部イベントスパンをエクスポート手前で間引く。
        # 新 SDK では span_exporter が read-only プロパティになっているため、
        # 内部の _batch_processor._exporter に直接書き込む経路で対応する。
        _wrap_span_exporters_with_noise_filter()
        logger.warning("AWS Application Signals enabled via aws-opentelemetry-distro")
    else:
        # ローカル開発：エクスポートせず、自動計装だけ有効にする。
        resource = Resource.create(
            {
                "service.name": service_name,
                "deployment.environment": environment,
            }
        )
        provider = TracerProvider(
            resource=resource,
            id_generator=AwsXRayIdGenerator(),
        )
        trace.set_tracer_provider(provider)
        logger.info(
            "OTLP endpoint not set; spans will not be exported "
            "(local development mode)."
        )

    # 自動計装：
    # - FastAPI: 受信リクエスト（/health は excluded_urls で計装対象外）
    # - botocore: boto3 が呼ぶ AWS API（DynamoDB など）
    # - httpx: 外部 HTTP API 呼び出し（/configuration から ipify を叩く等）
    FastAPIInstrumentor.instrument_app(app, excluded_urls=_EXCLUDED_URLS)
    BotocoreInstrumentor().instrument()
    HTTPXClientInstrumentor().instrument()


def _log_registered_span_processors() -> None:
    """distro が TracerProvider に登録した SpanProcessor の型・属性を出力する。

    切り分け用の一時的なデバッグ関数。``_span_processors`` リスト内に
    何が入っているか、``span_exporter`` を持つか、をログから確認できる。
    原因が判明したら削除する想定。

    Python の logging はデフォルト WARNING レベル以上しか出さないため、
    確実に awslogs に流すよう敢えて ``warning`` で出力する（一時運用）。
    """
    provider = trace.get_tracer_provider()
    logger.warning("DEBUG[telemetry]: TracerProvider type: %s", type(provider).__name__)
    multi = getattr(provider, "_active_span_processor", None)
    if multi is None:
        logger.warning("DEBUG[telemetry]: provider._active_span_processor is None")
        return
    logger.warning(
        "DEBUG[telemetry]: MultiSpanProcessor type: %s", type(multi).__name__
    )
    span_processors = getattr(multi, "_span_processors", None)
    if span_processors is None:
        logger.warning("DEBUG[telemetry]: multi._span_processors is None")
        return
    logger.warning(
        "DEBUG[telemetry]: Registered SpanProcessors count: %d", len(span_processors)
    )
    for i, sp in enumerate(span_processors):
        # 新しい SDK では BatchSpanProcessor.span_exporter は read-only property
        # で内部は _batch_processor._exporter にある。両方の経路を試す。
        exporter = getattr(sp, "span_exporter", None)
        if exporter is None:
            inner_batch = getattr(sp, "_batch_processor", None)
            exporter = getattr(inner_batch, "_exporter", None) if inner_batch else None
            exporter_source = "_batch_processor._exporter"
        else:
            exporter_source = "span_exporter"
        logger.warning(
            "DEBUG[telemetry]:   [%d] %s.%s (exporter=%s via=%s)",
            i,
            type(sp).__module__,
            type(sp).__name__,
            type(exporter).__name__ if exporter is not None else "None",
            exporter_source if exporter is not None else "-",
        )


def _wrap_span_exporters_with_noise_filter() -> None:
    """distro が登録した BatchSpanProcessor の SpanExporter を NoiseFilter で
    ラップする。

    distro は TracerProvider に Application Signals 用の SpanProcessor 群
    （``AwsSpanMetricsProcessor``、``AttributePropagatingSpanProcessor`` 等）と、
    エクスポート用の ``BatchSpanProcessor`` を登録する。``BatchSpanProcessor``
    自体は置き換えず、その内部の SpanExporter のみを ``_NoiseFilteringSpanExporter``
    で差し替えることで、distro が X-Ray リモートサンプラーの動的調整に使う
    ``BatchSpanProcessor`` 参照と競合させずに ASGI 内部イベントスパンを抑制する。

    新しい ``opentelemetry-sdk`` では ``BatchSpanProcessor.span_exporter`` は
    read-only プロパティ（実体は ``_batch_processor._exporter``）になっており、
    setter で代入できないため、内部の ``_batch_processor._exporter`` に直接
    書き込む。``opentelemetry-sdk`` のバージョン固定で互換性を担保する。
    """
    provider = trace.get_tracer_provider()
    multi = getattr(provider, "_active_span_processor", None)
    span_processors = getattr(multi, "_span_processors", None) if multi else None
    if not span_processors:
        logger.warning(
            "Could not access internal SpanProcessor list; "
            "NoiseFilter not applied (SDK version mismatch?)."
        )
        return

    found = False
    for sp in span_processors:
        if not isinstance(sp, BatchSpanProcessor):
            continue
        original = getattr(sp, "span_exporter", None)
        if original is None:
            continue
        wrapped = _NoiseFilteringSpanExporter(original)
        # まずは setter 経由（旧 SDK）。read-only property の場合は
        # 内部の _batch_processor._exporter に直接書き込む（新 SDK）。
        try:
            sp.span_exporter = wrapped
            found = True
            continue
        except AttributeError:
            pass
        inner_batch = getattr(sp, "_batch_processor", None)
        if inner_batch is not None and hasattr(inner_batch, "_exporter"):
            inner_batch._exporter = wrapped
            found = True
        else:
            logger.warning(
                "Cannot rewrite span_exporter on %s (no setter and no "
                "_batch_processor._exporter).",
                type(sp).__name__,
            )
    if not found:
        logger.warning(
            "No BatchSpanProcessor with exporter found in distro's "
            "TracerProvider; NoiseFilter not applied."
        )

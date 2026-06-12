# python-api-apm-sample-for-ecs-by-claudecode

FastAPI で実装したユーザー管理 API を、AWS ALB + ECS Fargate + DynamoDB 上で動かすサンプル。

## 構成図（論理）

```
[Internet] → ALB (public subnet) → ECS Fargate Task (private subnet) → DynamoDB (Gateway VPC Endpoint)
                                                              ↘ NAT Gateway (single AZ) → Internet
```

- リージョン: `ap-northeast-1`（東京）
- ネットワーク: 自前の VPC（Public/Private 2 AZ 構成）。コスト削減のため NAT Gateway は単一 AZ。
- DB: DynamoDB（Partition Key = `email`）。VPC からは Gateway 型エンドポイント経由。
- 認証: なし（サンプル用途）。

## ディレクトリ

| パス | 説明 |
| --- | --- |
| `app/` | FastAPI アプリケーション本体 |
| `app/api/` | ルーター層（`/users`） |
| `app/services/` | ドメインロジック（DynamoDB 入出力） |
| `app/models/` | Pydantic スキーマ |
| `app/db/` | boto3 クライアント生成 |
| `app/core/` | 設定 (`pydantic-settings`) |
| `Dockerfile` | uv で依存解決 → 非 root で uvicorn 起動 |
| `cloudformation/` | スタック分割した CFN テンプレート |
| `scripts/` | ビルド・デプロイ用シェルスクリプト |

## ローカル開発

```bash
uv sync
uv run uvicorn app.main:app --reload
# 別ターミナル: http://localhost:8000/docs を開く
```

DynamoDB Local を使う場合は `DYNAMODB_ENDPOINT_URL=http://localhost:8000` を設定。

## API 一覧

| Method | Path | 概要 |
| --- | --- | --- |
| GET | `/health` | ALB ヘルスチェック |
| POST | `/users` | ユーザー新規作成 |
| GET | `/users` | ユーザー一覧取得（最大 100 件） |
| GET | `/users/{email}` | ユーザー詳細取得 |
| PUT | `/users/{email}` | ユーザー名更新 |
| GET | `/configuration` | サンプル設定取得（外部 HTTP 呼び出しのトレース確認用） |

## デプロイ

AWS 認証情報（CLAUDE.md の指示通り、環境変数で渡す）と Docker / AWS CLI が必要。

```bash
export AWS_ACCESS_KEY_ID=...
export AWS_SECRET_ACCESS_KEY=...
# 必要に応じて
# export AWS_SESSION_TOKEN=...

# 1) VPC / ECR / DynamoDB を作成
./scripts/deploy-infra.sh

# 2) Docker イメージをビルドして ECR にプッシュ
./scripts/build.sh

# 3) ALB + ECS サービスをデプロイ（既存なら新タスクへロールアウト）
./scripts/deploy-service.sh

# まとめて：
./scripts/deploy-all.sh
```

完了後、`scripts/deploy-service.sh` の出力にある `http://<alb-dns>/docs` から Swagger UI を確認できる。

## トレーシング（AWS Application Signals）

ALB 経由のリクエストを受けた FastAPI から DynamoDB 呼び出しまでを **1 本のトレース** として
AWS Application Signals（X-Ray トレース詳細 + CloudWatch メトリクス）で可視化できるようにしている。
コードに専用 SDK（旧 X-Ray SDK 等）を持ち込まず、**OpenTelemetry 自動計装**＋
**CloudWatch Agent サイドカー（Application Signals モード）** の 2 段構成で実現しているのがポイント。

### データフロー

```
[Client]
    │ HTTP
    ▼
[ALB] ─── X-Amzn-Trace-Id 採番／伝播
    │
    ▼
┌─ ECS Fargate Task ────────────────────────────────────────────┐
│   ┌──────────────────┐  OTLP/HTTP  ┌────────────────────────┐ │
│   │ api (FastAPI)    │ ──────────▶ │ otel-collector         │ │
│   │ 自動計装:         │   :4316      │ (CloudWatch Agent      │ │
│   │  • FastAPI       │              │  Application Signals)  │ │
│   │  • botocore      │              └──────────┬─────────────┘ │
│   │  • httpx         │                         │               │
│   └──┬─────────┬─────┘                         │               │
└──────┼─────────┼───────────────────────────────┼───────────────┘
       │ AWS API │ HTTPS                          │
       │ (boto3) │ (httpx)              ┌─────────┴──────────┐
       ▼         ▼                      ▼                    ▼
   [DynamoDB]  [NAT GW] ──▶ Internet  [AWS X-Ray]   [CloudWatch Metrics]
   (Gateway               ──▶ [外部 API]  (トレース詳細)  (RED メトリクス)
    VPCE)                    (例: api.ipify.org)
```

### 構成要素

| レイヤ | 採用したもの | 役割 |
| --- | --- | --- |
| トレース ID | `AwsXRayIdGenerator` | `1-{8桁epoch}-{96bitランダム}` 形式で採番し X-Ray にそのまま投入できる |
| 伝播ヘッダ | `AwsXRayPropagator`（`X-Amzn-Trace-Id`） | ALB が付ける `X-Amzn-Trace-Id` を親コンテキストとして引き継ぎ、ALB→ECS でトレースが分断されない |
| アプリ計装 | `opentelemetry-instrumentation-fastapi` | 受信リクエストを HTTP サーバースパンに変換 |
| AWS SDK 計装 | `opentelemetry-instrumentation-botocore` | boto3 が呼ぶ DynamoDB API を AWS スパン（`AWS::DynamoDB::Table` ノード）に変換 |
| 外部 HTTP 計装 | `opentelemetry-instrumentation-httpx` | `httpx` 経由の外部 API 呼び出しを HTTP クライアントスパンに変換（`/configuration` で確認可） |
| 送信 | OTLP/HTTP（`opentelemetry-exporter-otlp-proto-http`） | `localhost:4316/v1/traces`（CloudWatch Agent Application Signals ポート）にバッチ送信 |
| 集約／Application Signals 送信 | CloudWatch Agent（`public.ecr.aws/cloudwatch-agent/cloudwatch-agent`）サイドカー | OTLP を受け取り X-Ray にトレースを PUT、CloudWatch に RED メトリクスを書き込む |

### Fargate サイドカー構成（`cloudformation/04-ecs-alb.yaml`）

- 同一タスク内に `api` コンテナと `otel-collector`（CloudWatch Agent）コンテナを定義。
  `awsvpc` モードのため両者は `localhost` で疎通する
  （`OTEL_EXPORTER_OTLP_TRACES_ENDPOINT=http://localhost:4316/v1/traces`）。
- CloudWatch Agent は `CW_CONFIG_CONTENT` 環境変数に JSON で設定を流し込む。
  `traces.traces_collected.application_signals` と `logs.metrics_collected.application_signals`
  を有効化するだけで OTLP receiver（ポート 4316）と X-Ray サンプリングプロキシ（UDP/2000）
  が起動する。外部設定ファイルは不要。
- `api` には `dependsOn: [{otel-collector, START}]` を付け、初回スパン送信時の接続失敗を回避。
- タスクロールに以下のマネージドポリシーを付与し、サイドカーから X-Ray / CloudWatch に
  書き込みできるようにしている。
    - `AWSXRayDaemonWriteAccess`（`xray:PutTraceSegments`, `xray:PutTelemetryRecords`）
    - `CloudWatchAgentServerPolicy`（メトリクス／ログ送信 + Application Signals API 呼び出し）
- サイドカー追加に伴い、タスクサイズは最小の `256/512` から `512/1024` に一段引き上げた。
  Agent 自体の常駐メモリ（〜50MiB）と DynamoDB スパンのバッファを安定して扱うため。

### アプリ側コード（`app/core/telemetry.py`）

`setup_tracing()` を `app/main.py` から 1 回だけ呼ぶ。やっていることは次の 4 ステップ：

1. `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` が設定されていれば `aws-opentelemetry-distro` の
   `AwsOpenTelemetryDistro` + `AwsOpenTelemetryConfigurator` を呼び出し、Application Signals
   用の TracerProvider / MeterProvider を構築する。未設定時（ローカル開発）は最小限の
   TracerProvider を自前で組み、外部接続を試みない。
2. distro による FastAPI/ASGI 自動計装は `OTEL_PYTHON_DISABLED_INSTRUMENTATIONS=fastapi,asgi`
   で無効化し、`FastAPIInstrumentor.instrument_app(app, excluded_urls=r"^/health$")` で
   手動計装する。これにより `/health` を確実に計装対象（= スパン生成 + メトリクス集計）から外す。
3. `BotocoreInstrumentor().instrument()` と `HTTPXClientInstrumentor().instrument()` で
   DynamoDB・外部 HTTP を自動計装する。
4. distro が登録した `BatchSpanProcessor` の `span_exporter` を `_NoiseFilteringSpanExporter`
   でラップし、ASGI 内部イベントスパンをエクスポート直前に drop する。

ECS 側で渡している環境変数（タスク定義より）：

| 変数 | 値 | 効果 |
| --- | --- | --- |
| `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` | `http://localhost:4316/v1/traces` | CloudWatch Agent Application Signals ポート宛 OTLP/HTTP |
| `OTEL_EXPORTER_OTLP_PROTOCOL` | `http/protobuf` | gRPC ではなく HTTP/Protobuf |
| `OTEL_AWS_APPLICATION_SIGNALS_ENABLED` | `true` | Application Signals のメトリクス派生を有効化 |
| `OTEL_AWS_APPLICATION_SIGNALS_EXPORTER_ENDPOINT` | `http://localhost:4316/v1/metrics` | メトリクス送信先 |
| `OTEL_METRICS_EXPORTER` | `none` | distro 既定の OTLP metrics exporter を無効化（Application Signals 経由で送るため） |
| `OTEL_LOGS_EXPORTER` | `none` | 同上（logs） |
| `OTEL_TRACES_SAMPLER` | `xray` | X-Ray セントラルサンプリングを使用 |
| `OTEL_TRACES_SAMPLER_ARG` | `endpoint=http://localhost:2000` | CW Agent が UDP/2000 で X-Ray と橋渡し |
| `OTEL_PYTHON_DISTRO` | `aws_distro` | aws-opentelemetry-distro の Configurator を有効化 |
| `OTEL_PYTHON_CONFIGURATOR` | `aws_configurator` | 同上 |
| `OTEL_SERVICE_NAME` | `py-apm-sample-api` | Application Signals サービスマップのノード名 |
| `OTEL_PROPAGATORS` | `tracecontext,baggage,b3,xray` | Application Signals 推奨の伝播形式 |
| `OTEL_RESOURCE_ATTRIBUTES` | `deployment.environment=prod,service.namespace=...` | サービスマップのフィルタタグ |

### ノイズスパンの間引き

X-Ray トレース詳細と Application Signals メトリクスを読みやすく保つため、
2 種類のアプローチでノイズを排除している。

#### 1. `/health` ヘルスチェック（計装自体をスキップ）

`FastAPIInstrumentor.instrument_app(app, excluded_urls=r"/health$")` により、
`/health` への受信リクエストはスパンが生成されない。スパンが生成されないため
Application Signals のメトリクス（リクエスト数・レイテンシ・エラー率）にも
ヘルスチェックが混入しない。

distro が FastAPI/ASGI を先に自動計装してしまうと `excluded_urls` が無効になるため、
distro 起動前に `OTEL_PYTHON_DISABLED_INSTRUMENTATIONS=fastapi,asgi` を設定し、
distro による自動計装をスキップしている。

#### 2. ASGI 内部イベントスパン（エクスポート直前に drop）

`FastAPIInstrumentor` 配下の `opentelemetry-instrumentation-asgi` は、
1 リクエストにつき以下のサブスパンを自動生成する。

| スパン名 | `asgi.event.type` 属性 | 何のタイミング |
| --- | --- | --- |
| `... http receive` | `http.request` | リクエストボディ受信 |
| `... http send` | `http.response.start` | ステータスコード＋ヘッダ送信 |
| `... http send` | `http.response.body` | ボディチャンク送信 |

これらは X-Ray のサブセグメントとして冗長に並ぶため、
`_NoiseFilteringSpanExporter` が `asgi.event.type` 属性を持つスパンをエクスポート
直前に drop する。distro が登録した `BatchSpanProcessor` の `span_exporter` を
このラッパーで差し替えており、`BatchSpanProcessor` 自体は触らないため
X-Ray リモートサンプラー連携と競合しない。

### 外部 HTTP 呼び出しの計装（httpx）

`opentelemetry-instrumentation-httpx` を `setup_tracing()` 内で有効化しているため、
**`httpx.Client` / `httpx.AsyncClient` から発行される全ての HTTP リクエストが
自動的にクライアントスパン化される**。アプリケーション側のコードに変更は不要で、
`async with httpx.AsyncClient() as client: await client.get(...)` のような通常の
書き方でそのまま観測できる。

#### スパンの中身

| 観点 | 内容 |
| --- | --- |
| Span Kind | `CLIENT` |
| Span 名 | HTTP メソッド名（`GET` / `POST` …） |
| 主な属性 | `http.url`, `http.method`, `http.status_code`, `server.address`, `server.port` |
| 親スパン | 同リクエスト内で生成された `SERVER` スパン（例: `GET /configuration`） |
| サービスマップ | 接続先ホスト名（`api.ipify.org` 等）が外部ノードとして可視化 |

#### トレースコンテキストの伝播

`AwsXRayPropagator` をグローバルプロパゲーターに設定しているため、httpx で
発行する**外部リクエストのヘッダにも `X-Amzn-Trace-Id` が自動で付与される**。
ipify のような汎用 API 側はこのヘッダを使わないが、相手側も
OpenTelemetry/X-Ray で計装されているマイクロサービスであれば、
**追加実装なしで分散トレーシングが連結する**（`/configuration` ⇆ 別の社内 API
というパターンに発展させる場合も、本サンプルの構成のまま動作する）。

#### サンプル：`GET /configuration`

`app/api/configuration.py` で `httpx.AsyncClient` から `https://api.ipify.org`
を呼び出して結果を返すだけの薄いエンドポイント。機能的な意味は薄く、
**httpx 計装の動作確認専用**として用意している。

```bash
curl "http://${ALB_DNS}/configuration"
# => {"service_name":"py-apm-sample","environment":"prod","outbound_ip":"<NAT GW の EIP>"}
```

X-Ray 上で見えるべきもの：

- **トレース詳細**: ルートの `GET /configuration`（SERVER スパン）配下に、
  `GET`（CLIENT スパン、`http.url=https://api.ipify.org/?format=json`）と、
  後述の `calc_configuration`（手動スパン、約 500ms）が並んで表示される。
  HTTP ステータスや接続先ホストも属性として記録されている。
- **サービスマップ**: `py-apm-sample-api → api.ipify.org` のエッジが追加される。
  外部 API がレイテンシスパイク／エラーになるとこのエッジ上で可視化される。
- **副次効果**: レスポンスの `outbound_ip` は NAT Gateway に紐付いた Elastic IP と
  一致するので、**Private Subnet → NAT → Internet** の経路まで合わせて検証できる。

#### 別の HTTP クライアントを使いたい場合

| ライブラリ | 必要な計装パッケージ |
| --- | --- |
| `httpx`（採用中） | `opentelemetry-instrumentation-httpx` |
| `requests` | `opentelemetry-instrumentation-requests` |
| `urllib`/`urllib3` | `opentelemetry-instrumentation-urllib` / `-urllib3` |
| `aiohttp` クライアント | `opentelemetry-instrumentation-aiohttp-client` |

いずれも `setup_tracing()` 内で `XxxInstrumentor().instrument()` を 1 行追加する
だけで計装できる。

### 独自関数の手動スパン化（X-Ray サブセグメント）

自動計装ではカバーされない**独自のアプリケーション関数**を X-Ray のサブセグメント
として計測したい場合は、OpenTelemetry の `tracer.start_as_current_span()` で
スコープを切るだけで良い。`setup_tracing()` で構築済みの `TracerProvider` を
経由するため、追加のセットアップやエクスポーター設定は不要。

サンプル実装は `app/api/configuration.py` の `calc_configuration` 関数：

```python
from opentelemetry import trace

# モジュール先頭で 1 度だけ tracer を取得（__name__ を渡すと発行元が分かる）
tracer = trace.get_tracer(__name__)

def calc_configuration():
    """ダミー処理。X-Ray 上でサブセグメントとして可視化するだけが目的。"""
    with tracer.start_as_current_span("calc_configuration"):
        logger.info("Call function calc_configuration.")
        time.sleep(0.5)
```

`GET /configuration` ハンドラ内で `return` の直前に `calc_configuration()` を
呼び出している。

#### 仕組み

| 観点 | 内容 |
| --- | --- |
| Span 名 | `tracer.start_as_current_span("calc_configuration")` の引数。X-Ray のサブセグメント名としてそのまま表示される |
| 親スパン | `with` に入った時点で「現在アクティブなスパン」（= FastAPIInstrumentor が発行する `GET /configuration` の SERVER スパン）が自動的に親になる |
| Span Kind | 既定の `INTERNAL`（外部呼び出しではないため） |
| Context 伝播 | `async` ハンドラから同期関数を呼んでも OpenTelemetry の context（`contextvars` ベース）はそのまま引き継がれるため、親子関係が維持される |
| エクスポート | 他のスパンと同じく `BatchSpanProcessor` → OTLP/HTTP → CloudWatch Agent → Application Signals の経路で送られる |
| ノイズフィルタ | `_NoiseFilteringSpanExporter` の drop 対象は `asgi.event.type` 属性を持つ ASGI 内部イベントのみ。手動スパンが意図せず捨てられることはない |

#### X-Ray 上で見えるもの

`GET /configuration` のトレース詳細を開くと、SERVER スパン配下に以下が並ぶ：

```
GET /configuration                          (SERVER, ~500ms+)
├─ GET https://api.ipify.org/?format=json   (CLIENT, httpx 自動計装)
└─ calc_configuration                       (INTERNAL, 手動スパン, ~500ms)
```

例外を投げた場合は `with` ブロックを抜ける際に span status が自動で `ERROR` になり、
X-Ray のサブセグメントが赤くハイライトされる（明示的な `record_exception()` 呼び出しは
不要）。

#### 属性を足したい場合

業務ロジックの入出力をスパンに乗せたい場合は `set_attribute` で属性を足せる：

```python
with tracer.start_as_current_span("calc_configuration") as span:
    span.set_attribute("config.input_size", len(payload))
    result = do_work(payload)
    span.set_attribute("config.output_size", len(result))
```

X-Ray のサブセグメント詳細パネルの "Annotations / Metadata" に表示される
（OTLP → X-Ray 変換時のキー長やインデックス可否ルールがあるため、検索対象にしたい
キーは英数アンダースコアの短い名前にしておくと安全）。

### 動作確認

デプロイ後、いくつかリクエストを投げてから X-Ray コンソールを開く：

```bash
curl -X POST "http://${ALB_DNS}/users" -H 'content-type: application/json' \
  -d '{"email":"alice@example.com","name":"Alice"}'
curl "http://${ALB_DNS}/users/alice@example.com"
```

- **Service map**: `py-apm-sample-api` ノードと `DynamoDB` ノードがエッジで接続される。
- **Traces**: 1 リクエストにつき `GET /users/{email}` のサーバースパンと、その配下に
  `DynamoDB.GetItem` の AWS スパンがぶら下がる。
- ALB 自体は X-Ray に独立スパンを発行しないが、ALB が採番した `X-Amzn-Trace-Id` を
  アプリ側で親として継承するため、トレース ID は ALB から DynamoDB まで一貫する。

外部 HTTP 呼び出しが計装されていることの確認は前述の
[外部 HTTP 呼び出しの計装（httpx）](#外部-http-呼び出しの計装httpx) を参照。

### 既知のはまりどころ

- ECR Public からの `cloudwatch-agent` イメージ pull は、Private サブネットからは
  NAT Gateway 経由で外部に出る必要がある。本サンプルは NAT Gateway を構築済みなので OK。
- アプリコンテナが CloudWatch Agent より先に立ち上がると初回エクスポートが失敗するため、
  `dependsOn: START` を必ず指定する（再起動時のレース対策）。
- ローカル実行（`uv run uvicorn ...`）では `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` を未設定にしておけば
  Agent への接続を試みず、自動計装のオーバーヘッドだけが乗る状態で開発できる。
- `OTEL_PYTHON_DISABLED_INSTRUMENTATIONS=fastapi,asgi` は distro を呼ぶ前にコード内で
  `os.environ.setdefault` で設定している。ECS タスク定義から同名の環境変数を渡せば上書きできる。
- httpx 計装は `HTTPXClientInstrumentor().instrument()` を呼んだ時点でグローバルに
  パッチが当たる。既に生成済みの `Client` インスタンスにも反映されるが、計装前から
  保持していた接続のリクエストではコンテキストが伝播しないので、`setup_tracing()` は
  必ず最初に呼ぶこと。

## クリーンアップ

```bash
aws cloudformation delete-stack --stack-name py-apm-sample-ecs
aws cloudformation delete-stack --stack-name py-apm-sample-dynamodb
aws cloudformation delete-stack --stack-name py-apm-sample-ecr
aws cloudformation delete-stack --stack-name py-apm-sample-network
```

NAT Gateway / EIP は時間課金のため、不要時は削除すること。

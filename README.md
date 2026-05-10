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

## トレーシング（AWS Distro for OpenTelemetry → X-Ray）

ALB 経由のリクエストを受けた FastAPI から DynamoDB 呼び出しまでを **1 本のトレース** として
AWS X-Ray のサービスマップ／トレース詳細で可視化できるようにしている。
コードに専用 SDK（旧 X-Ray SDK 等）を持ち込まず、**OpenTelemetry の自動計装**＋
**ADOT Collector サイドカー**の 2 段構成で実現しているのがポイント。

### データフロー

```
[Client]
   │  (HTTP)
   ▼
[ALB]                       ← X-Amzn-Trace-Id を採番／伝播
   │  (HTTP, X-Amzn-Trace-Id)
   ▼
┌─ ECS Fargate Task ──────────────────────────────┐
│  ┌──────────────┐  OTLP/HTTP   ┌──────────────┐ │
│  │  api (FastAPI)│ ───────────▶│ otel-collector│ │
│  │  自動計装:    │  :4318       │ (ADOT sidecar)│ │
│  │   - FastAPI   │              └──────┬───────┘ │
│  │   - botocore  │                     │         │
│  └──────┬────────┘                     │         │
└─────────┼──────────────────────────────┼─────────┘
          │ DynamoDB API                 │ awsxray exporter
          ▼                              ▼
     [DynamoDB]                     [AWS X-Ray]
```

### 構成要素

| レイヤ | 採用したもの | 役割 |
| --- | --- | --- |
| トレース ID | `AwsXRayIdGenerator` | `1-{8桁epoch}-{96bitランダム}` 形式で採番し X-Ray にそのまま投入できる |
| 伝播ヘッダ | `AwsXRayPropagator`（`X-Amzn-Trace-Id`） | ALB が付ける `X-Amzn-Trace-Id` を親コンテキストとして引き継ぎ、ALB→ECS でトレースが分断されない |
| アプリ計装 | `opentelemetry-instrumentation-fastapi` | 受信リクエストを HTTP サーバースパンに変換 |
| AWS SDK 計装 | `opentelemetry-instrumentation-botocore` | boto3 が呼ぶ DynamoDB API を AWS スパン（`AWS::DynamoDB::Table` ノード）に変換 |
| 外部 HTTP 計装 | `opentelemetry-instrumentation-httpx` | `httpx` 経由の外部 API 呼び出しを HTTP クライアントスパンに変換（`/configuration` で確認可） |
| 送信 | OTLP/HTTP（`opentelemetry-exporter-otlp-proto-http`） | `localhost:4318` のサイドカー宛にバッチ送信 |
| 集約／X-Ray 送信 | ADOT Collector（`public.ecr.aws/aws-observability/aws-otel-collector`）サイドカー | OTLP を受け取り `awsxray` exporter で X-Ray API に PUT |

### Fargate サイドカー構成（`cloudformation/04-ecs-alb.yaml`）

- 同一タスク内に `api` コンテナと `otel-collector` コンテナを定義。`awsvpc` モードのため
  両者は `localhost` で疎通する（`OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318`）。
- ADOT 公式イメージ同梱の既定設定 `/etc/ecs/ecs-default-config.yaml` を `--config` で指定。
  これだけで OTLP receiver (`4317` / `4318`) と `awsxray` / `awsemf` exporter が有効になる。
- `api` には `dependsOn: [{otel-collector, START}]` を付け、初回スパン送信時の接続失敗を回避。
- タスクロールに以下のマネージドポリシーを付与し、サイドカーから X-Ray / CloudWatch に
  書き込みできるようにしている。
    - `AWSXRayDaemonWriteAccess`（`xray:PutTraceSegments`, `xray:PutTelemetryRecords`）
    - `CloudWatchAgentServerPolicy`（メトリクス／ログ送信用）
- サイドカー追加に伴い、タスクサイズは最小の `256/512` から `512/1024` に一段引き上げた。
  Collector 自体の常駐メモリ（〜50MiB）と DynamoDB スパンのバッファを安定して扱うため。

### アプリ側コード（`app/core/telemetry.py`）

`setup_tracing()` を `app/main.py` から 1 回だけ呼ぶ。やっていることは次の 5 ステップ：

1. グローバルプロパゲーターを `AwsXRayPropagator` に差し替え（ALB の `X-Amzn-Trace-Id` を解釈）
2. `service.name` / `deployment.environment` を `Resource` として設定
3. `id_generator=AwsXRayIdGenerator()` を持つ `TracerProvider` を構築
4. `OTEL_EXPORTER_OTLP_ENDPOINT` が設定されていれば OTLP/HTTP の `BatchSpanProcessor` を装着
   （ローカル開発時は未設定にしておけば送信処理ごと無効化される）
5. `FastAPIInstrumentor.instrument_app(app)` と `BotocoreInstrumentor().instrument()` で自動計装

ECS 側で渡している環境変数（タスク定義より）：

| 変数 | 値 | 効果 |
| --- | --- | --- |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `http://localhost:4318` | サイドカー Collector 宛 OTLP/HTTP |
| `OTEL_EXPORTER_OTLP_PROTOCOL` | `http/protobuf` | gRPC ではなく HTTP/Protobuf |
| `OTEL_SERVICE_NAME` | `py-apm-sample-api` | X-Ray サービスマップのノード名 |
| `OTEL_PROPAGATORS` | `xray` | コード側設定の保険として明示 |
| `OTEL_RESOURCE_ATTRIBUTES` | `deployment.environment=prod,service.namespace=...` | サービスマップのフィルタタグ |

### ノイズスパンの間引き

X-Ray のトレース一覧／トレース詳細を読みやすく保つため、
`app/core/telemetry.py` の `_NoiseSpanFilterProcessor` で `BatchSpanProcessor`
を委譲ラップし、エクスポート直前に 2 種類のノイズを捨てている。

#### 1. ASGI 内部イベントスパン（常に捨てる）

`FastAPIInstrumentor` 配下の `opentelemetry-instrumentation-asgi` は、
1 リクエストにつき以下のサブスパンを自動生成する。

| スパン名 | `asgi.event.type` 属性 | 何のタイミング |
| --- | --- | --- |
| `... http receive` | `http.request` | リクエストボディ受信 |
| `... http send` | `http.response.start` | ステータスコード＋ヘッダ送信 |
| `... http send` | `http.response.body` | ボディチャンク送信 |

これらは TTFB / ボディフラッシュの厳密測定に有用だが、X-Ray のサブセグメント
として冗長に並んで可読性を落とす。`asgi.event.type` 属性を持つスパンは
無条件で export 対象から外している。

#### 2. ヘルスチェックの成功スパン

ALB のターゲットヘルスチェックは秒〜数十秒間隔で `/health` を叩くため、
**`/health` への成功スパンのみ** 捨てる。

| 条件 | X-Ray に出るか |
| --- | --- |
| `GET /health` が 200 で返る | ✗（捨てる） |
| `GET /health` が 5xx を返す | ✓（残す） |
| `GET /health` 内で例外が上がり span status が ERROR になる | ✓（残す） |
| その他のパス（`/users` など） | ✓（常に残す） |

`Sampler` ではなく `SpanProcessor` 段で判定しているのは、サンプラーがスパン
**開始時**に判定するため「成功なら捨てる」という後追い判断ができないため。

対象パスを増やしたい場合は
`setup_tracing(app, ..., drop_successful_paths=("/health", "/ping"))` のように
呼び出し側で渡せる。

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

外部 HTTP 呼び出しのトレースが乗っていることは `/configuration` で確認できる：

```bash
curl "http://${ALB_DNS}/configuration"
# => {"service_name":"...","environment":"prod","outbound_ip":"<NAT GW の EIP>"}
```

このリクエストでは `GET /configuration` のサーバースパン配下に
`GET https://api.ipify.org` の HTTP クライアントスパンが付き、X-Ray のサービスマップに
`api.ipify.org`（外部 HTTP）ノードが追加される。`outbound_ip` の値が NAT Gateway に
紐付いた EIP と一致することで、Private Subnet → NAT → Internet の経路も
合わせて検証できる。

### 既知のはまりどころ

- ECR Public からの `aws-otel-collector` イメージ pull は、Private サブネットからは
  NAT Gateway 経由で外部に出る必要がある。本サンプルは NAT Gateway を構築済みなので OK。
- アプリコンテナが Collector より先に立ち上がると初回エクスポートが失敗するため、
  `dependsOn: START` を必ず指定する（再起動時のレース対策）。
- ローカル実行（`uv run uvicorn ...`）では `OTEL_EXPORTER_OTLP_ENDPOINT` を未設定にしておけば
  Collector への接続を試みず、自動計装のオーバーヘッドだけが乗る状態で開発できる。

## クリーンアップ

```bash
aws cloudformation delete-stack --stack-name py-apm-sample-ecs
aws cloudformation delete-stack --stack-name py-apm-sample-dynamodb
aws cloudformation delete-stack --stack-name py-apm-sample-ecr
aws cloudformation delete-stack --stack-name py-apm-sample-network
```

NAT Gateway / EIP は時間課金のため、不要時は削除すること。

# CLAUDE.md — Backend開発ガイド

## プロジェクト概要
* Fast APIによるバックエンドAPIを開発する。  
* インフラはAWSを利用し、ALBとECSを利用する。
* インフラコードの管理は、CloudFormation Templateを利用する。
* APM（分散トレーシング）には OpenTelemetry（`aws-opentelemetry-distro`）を利用し、AWS Application Signals（X-Ray トレース + CloudWatch メトリクス）で可視化する。

## 技術スタック
| 用途          | ライブラリ                             |
|-------------|-----------------------------------|
| Webフレームワーク  | FastAPI                           |
| インフラストラクチャー | AWS（ALB、ECSを中心に利用）                |
| インフラコード管理   | CloudFormation Template           |
| DB | DynamoDB |
| 外部HTTPクライアント | httpx |
| 分散トレーシング SDK | OpenTelemetry（API/SDK、自動計装：fastapi / botocore / httpx） |
| トレース ID 仕様 | AWS X-Ray ID Generator + AWS X-Ray Propagator（`X-Amzn-Trace-Id`） |
| トレース集約／送信 | CloudWatch Agent（ECS Fargate サイドカー・Application Signals モード） |
| 可視化 | AWS Application Signals（サービスマップ・RED メトリクス・X-Ray トレース詳細） |

## ディレクトリ構成
* Pythonコードは、/app 配下に作成すること。
* DockerfileやCloudFormation Templateは、ルートディレクトリ直下に作成すること。
* トレーシング初期化コードは `app/core/telemetry.py` に集約する。

## バックエンドAPIの機能
* ユーザー管理を行う。新規作成、更新、詳細取得、一覧取得。
* `GET /configuration` は外部 HTTP API（ipify）を呼び出して結果を返すサンプル
  エンドポイント。機能的な意味は薄く、`httpx` 経由の外部 HTTP 呼び出しが
  X-Ray のサービスマップに外部ノードとして現れることを確認するための疎通用。

## インフラストラクチャー（AWS）の詳細
* AWS東京リージョンを利用する。
* VPC、サブネットは新規で作成する。
    * Publicサブネット、Privateサブネットの二層構造にする。
    * DynamoDBへの接続は、VPC Endpoint（Gateway）を利用する。
* 外部接続のためのNATGatewayを追加する。
    * ただし、コスト削減のため、単一ゾーンにのみ配置する。
* ALBを利用する。
    * 認証は不要とする。
    * ALB 自体は X-Ray にスパンを発行しないが、`X-Amzn-Trace-Id` ヘッダを採番／伝播するため、
      アプリ側で X-Ray Propagator を使うことで ALB → ECS のトレースを連結できる。
* ECS Fargateを利用する。
    * 基本方針はコスト削減のため最小の CPU/Memory を選ぶこと。
    * ただし CloudWatch Agent をサイドカーとして同居させる関係で、最小組み合わせ
      （256/512）から **512/1024** に一段引き上げている。Agent の常駐分
      （〜50MiB）と OOM 耐性を確保するための妥協であり、観測性が不要な場面では
      最小組み合わせに戻して構わない。
    * 同一タスク内に **アプリコンテナ** と **`otel-collector` サイドカー**
      （`public.ecr.aws/cloudwatch-agent/cloudwatch-agent`）を配置する。
      アプリ → サイドカーは `awsvpc` の `localhost:4316`（OTLP/HTTP、Application Signals ポート）
      で通信する。アプリには `dependsOn: { otel-collector: START }` を付け、
      初回スパン送信のレース失敗を回避する。
* Docker Imageの保持は、ECRを利用する。
* ユーザー情報の保存は、DynamoDBを利用する。
    * Partition Keyは、メールアドレスとする。
* Docker Build、AWS環境へのデプロイ用のShell Scriptを作成する。
    * AWSの認証は、環境変数からAWSアクセスキーとシークレットキーを取得して利用する。
* IAM
    * Task Role には DynamoDB の CRUD 権限に加え、CloudWatch Agent サイドカーが X-Ray への
      トレース書き込み・CloudWatch へのメトリクス書き込み・Application Signals API 呼び出しを
      行うための **`AWSXRayDaemonWriteAccess`** と **`CloudWatchAgentServerPolicy`** を付与する
      （タスクロールはコンテナ間で共有される）。

## トレーシング（APM）の方針
* `app/main.py` の起動時に `app.core.telemetry.setup_tracing()` を 1 回だけ呼ぶ。
  実装は次の 4 ステップ：
    1. `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` が設定されていれば、`aws-opentelemetry-distro`
       の `AwsOpenTelemetryDistro().configure()` + `AwsOpenTelemetryConfigurator().configure()`
       を呼び出し、Application Signals 用の TracerProvider / MeterProvider を構築する。
       未設定時（ローカル開発）は最小限の TracerProvider を自前で組み、外部接続を試みない。
    2. distro による FastAPI/ASGI 自動計装は `OTEL_PYTHON_DISABLED_INSTRUMENTATIONS=fastapi,asgi`
       で無効化し、`FastAPIInstrumentor.instrument_app(app, excluded_urls=r"/health$")` で
       手動計装する。これにより `/health` を確実に計装対象から外す。
    3. botocore（DynamoDB）・httpx（外部 HTTP）は通常どおり自動計装する。
    4. distro が登録した `BatchSpanProcessor` の `span_exporter` を `_NoiseFilteringSpanExporter`
       でラップし、ASGI 内部イベントスパン（`asgi.event.type` 属性を持つ `http send` /
       `http receive`）をエクスポート直前に drop する。`BatchSpanProcessor` 自体は触らない
       ため、distro が X-Ray リモートサンプラー連携に使う参照と競合しない。
* **`/health` の除外** は `FastAPIInstrumentor.instrument_app(excluded_urls=...)` でスパン
  生成自体をスキップする。Application Signals のメトリクス（リクエスト数・レイテンシ・
  エラー率）にもヘルスチェックが混入しない。失敗時も ALB の `UnHealthyHostCount`
  メトリクスで別途監視できる前提。
* 計装の追加・削除はアプリコードの編集だけで完結させる。CloudFormation には
  サイドカー定義以外のトレーシング固有設定を持たせない（CloudWatch Agent は
  `CW_CONFIG_CONTENT` 環境変数に JSON を渡すだけで Application Signals が有効になる）。
* ローカル開発時は `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` を未設定のままにすること。
  Collector への接続を試みず、自動計装オーバーヘッドのみが乗った状態で動作する。

# CLAUDE.md — Backend開発ガイド

## プロジェクト概要
* Fast APIによるバックエンドAPIを開発する。  
* インフラはAWSを利用し、ALBとECSを利用する。
* インフラコードの管理は、CloudFormation Templateを利用する。
* APM（分散トレーシング）には AWS Distro for OpenTelemetry（ADOT）を利用し、AWS X-Ray で可視化する。

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
| トレース集約／送信 | AWS Distro for OpenTelemetry Collector（ECS Fargate サイドカー） |
| 可視化 | AWS X-Ray（サービスマップ／トレース詳細） |

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
    * ただし ADOT Collector をサイドカーとして同居させる関係で、最小組み合わせ
      （256/512）から **512/1024** に一段引き上げている。Collector の常駐分
      （〜50MiB）と OOM 耐性を確保するための妥協であり、観測性が不要な場面では
      最小組み合わせに戻して構わない。
    * 同一タスク内に **アプリコンテナ** と **`otel-collector` サイドカー**
      （`public.ecr.aws/aws-observability/aws-otel-collector`）を配置する。
      アプリ → サイドカーは `awsvpc` の `localhost:4318`（OTLP/HTTP）で通信する。
      アプリには `dependsOn: { otel-collector: START }` を付け、初回スパン送信の
      レース失敗を回避する。
* Docker Imageの保持は、ECRを利用する。
* ユーザー情報の保存は、DynamoDBを利用する。
    * Partition Keyは、メールアドレスとする。
* Docker Build、AWS環境へのデプロイ用のShell Scriptを作成する。
    * AWSの認証は、環境変数からAWSアクセスキーとシークレットキーを取得して利用する。
* IAM
    * Task Role には DynamoDB の CRUD 権限に加え、サイドカーが X-Ray／CloudWatch に
      書き込むための **`AWSXRayDaemonWriteAccess`** と
      **`CloudWatchAgentServerPolicy`** を付与する（タスクロールはコンテナ間で共有される）。

## トレーシング（APM）の方針
* `app/main.py` の起動時に `app.core.telemetry.setup_tracing()` を 1 回だけ呼ぶ。
  実装は次の 5 ステップ：
    1. グローバルプロパゲーターを `AwsXRayPropagator` に差し替える（ALB の
       `X-Amzn-Trace-Id` を解釈してトレース ID を継承）。
    2. `service.name` / `deployment.environment` を `Resource` として設定する。
    3. `id_generator=AwsXRayIdGenerator()` を持つ `TracerProvider` を構築する
       （X-Ray 形式 `1-{8桁epoch}-{96bitランダム}`）。
    4. `OTEL_EXPORTER_OTLP_ENDPOINT`（タスク環境変数で `http://localhost:4318`）が
       設定されていれば OTLP/HTTP の `BatchSpanProcessor` を装着する。未設定時
       （ローカル開発）は接続を試みず、自動計装だけ有効にする。
    5. 自動計装：FastAPI（受信）／botocore（DynamoDB）／httpx（外部 HTTP）。
* **ノイズスパンの間引き** は `_NoiseSpanFilterProcessor` を `BatchSpanProcessor`
  の前段に挟むことで実装する：
    * ASGI 内部イベントスパン（`asgi.event.type` 属性を持つ `http send` /
      `http receive`）は無条件で drop する。
    * 既定で `/health` パスへの **成功スパンのみ** drop する（5xx・例外時は残す）。
    * `Sampler` ではなく `SpanProcessor` の `on_end` で判定するのは、サンプラーが
      スパン**開始時**に判定するため「成功なら捨てる」という後追いができないため。
* 計装の追加・削除はアプリコードの編集だけで完結させる。CloudFormation には
  サイドカー定義以外のトレーシング固有設定を持たせない（ADOT Collector は
  公式イメージ同梱の `/etc/ecs/ecs-default-config.yaml` をそのまま使う）。
* ローカル開発時は `OTEL_EXPORTER_OTLP_ENDPOINT` を未設定のままにすること。
  Collector への接続を試みず、自動計装オーバーヘッドのみが乗った状態で動作する。

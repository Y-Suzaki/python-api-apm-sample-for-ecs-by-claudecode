"""アプリケーション設定。環境変数から読み込む。"""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """ECSタスク定義の環境変数から注入される設定値。"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "python-api-apm-sample"
    app_env: str = "local"

    # AWS
    aws_region: str = "ap-northeast-1"

    # DynamoDB
    dynamodb_users_table: str = "users"
    # ローカル開発時に DynamoDB Local を指す場合に使う。本番では None。
    dynamodb_endpoint_url: str | None = None


@lru_cache
def get_settings() -> Settings:
    """設定をキャッシュして返す。FastAPIの依存性注入から利用する。"""
    return Settings()

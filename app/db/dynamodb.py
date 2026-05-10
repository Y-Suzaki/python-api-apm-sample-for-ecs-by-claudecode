"""DynamoDB クライアントの生成。FastAPI の Depends から利用する。"""
from functools import lru_cache

import boto3
from botocore.config import Config

from app.core.config import Settings, get_settings


@lru_cache
def _build_resource(region: str, endpoint_url: str | None):
    """boto3 resource を生成する。テストや Lambda コールド・スタートのため lru_cache。"""
    botocore_config = Config(
        retries={"max_attempts": 5, "mode": "standard"},
        connect_timeout=3,
        read_timeout=5,
    )
    return boto3.resource(
        "dynamodb",
        region_name=region,
        endpoint_url=endpoint_url,
        config=botocore_config,
    )


def get_users_table(settings: Settings | None = None):
    """users テーブルの Table オブジェクトを返す。"""
    settings = settings or get_settings()
    resource = _build_resource(settings.aws_region, settings.dynamodb_endpoint_url)
    return resource.Table(settings.dynamodb_users_table)

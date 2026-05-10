"""ユーザーのドメインモデル / スキーマ定義。"""
from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class UserBase(BaseModel):
    """ユーザーの共通フィールド。"""

    email: EmailStr = Field(..., description="ユーザーの一意キーとなるメールアドレス")
    name: str = Field(..., min_length=1, max_length=100, description="表示名")


class UserCreate(UserBase):
    """新規作成リクエストのボディ。"""

    pass


class UserUpdate(BaseModel):
    """更新リクエストのボディ。`name` のみ更新可能。"""

    name: str = Field(..., min_length=1, max_length=100)


class User(UserBase):
    """API レスポンスとして返却するユーザーモデル。"""

    model_config = ConfigDict(from_attributes=True)

    created_at: datetime
    updated_at: datetime

    @staticmethod
    def utcnow() -> datetime:
        """UTC のタイムゾーン付き現在時刻を返す。"""
        return datetime.now(timezone.utc)

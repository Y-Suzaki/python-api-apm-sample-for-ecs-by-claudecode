"""ユーザー管理のビジネスロジック層。DynamoDB アクセスをここに集約。"""
from typing import Any

from botocore.exceptions import ClientError

from app.models.user import User, UserCreate, UserUpdate


class UserAlreadyExistsError(Exception):
    """同一メールアドレスのユーザーが既に存在する場合に送出。"""


class UserNotFoundError(Exception):
    """指定メールアドレスのユーザーが存在しない場合に送出。"""


class UserService:
    """DynamoDB の users テーブルに対する CRUD を担う。"""

    def __init__(self, table) -> None:
        self._table = table

    # -- helpers ------------------------------------------------------------

    @staticmethod
    def _to_item(user: User) -> dict[str, Any]:
        return {
            "email": user.email,
            "name": user.name,
            "created_at": user.created_at.isoformat(),
            "updated_at": user.updated_at.isoformat(),
        }

    @staticmethod
    def _from_item(item: dict[str, Any]) -> User:
        return User.model_validate(item)

    # -- operations ---------------------------------------------------------

    def create(self, payload: UserCreate) -> User:
        """新規ユーザーを作成する。同一メールアドレスが既に存在する場合は例外。"""
        now = User.utcnow()
        user = User(
            email=payload.email,
            name=payload.name,
            created_at=now,
            updated_at=now,
        )
        try:
            self._table.put_item(
                Item=self._to_item(user),
                ConditionExpression="attribute_not_exists(email)",
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                raise UserAlreadyExistsError(payload.email) from exc
            raise
        return user

    def get(self, email: str) -> User:
        """メールアドレスでユーザーを取得する。"""
        resp = self._table.get_item(Key={"email": email})
        item = resp.get("Item")
        if not item:
            raise UserNotFoundError(email)
        return self._from_item(item)

    def list(self, limit: int = 50) -> list[User]:
        """ユーザー一覧を返す。サンプル実装のため Scan を使用。"""
        resp = self._table.scan(Limit=limit)
        return [self._from_item(i) for i in resp.get("Items", [])]

    def update(self, email: str, payload: UserUpdate) -> User:
        """ユーザーの `name` を更新する。存在しなければ例外。"""
        now = User.utcnow()
        try:
            resp = self._table.update_item(
                Key={"email": email},
                UpdateExpression="SET #n = :name, updated_at = :updated_at",
                ConditionExpression="attribute_exists(email)",
                ExpressionAttributeNames={"#n": "name"},
                ExpressionAttributeValues={
                    ":name": payload.name,
                    ":updated_at": now.isoformat(),
                },
                ReturnValues="ALL_NEW",
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                raise UserNotFoundError(email) from exc
            raise
        return self._from_item(resp["Attributes"])

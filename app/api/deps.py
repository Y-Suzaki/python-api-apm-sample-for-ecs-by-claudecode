"""FastAPI の依存性注入定義。"""
from typing import Annotated

from fastapi import Depends

from app.core.config import Settings, get_settings
from app.db.dynamodb import get_users_table
from app.services.user_service import UserService


def _settings_dep() -> Settings:
    return get_settings()


def _user_service_dep(
    settings: Annotated[Settings, Depends(_settings_dep)],
) -> UserService:
    return UserService(get_users_table(settings))


SettingsDep = Annotated[Settings, Depends(_settings_dep)]
UserServiceDep = Annotated[UserService, Depends(_user_service_dep)]

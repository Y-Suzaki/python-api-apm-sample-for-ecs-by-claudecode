"""ユーザー管理 API のルーター。"""
from fastapi import APIRouter, HTTPException, Query, status
from pydantic import EmailStr

from app.api.deps import UserServiceDep
from app.models.user import User, UserCreate, UserUpdate
from app.services.user_service import UserAlreadyExistsError, UserNotFoundError

router = APIRouter(prefix="/users", tags=["users"])


@router.post(
    "",
    response_model=User,
    status_code=status.HTTP_201_CREATED,
    summary="ユーザー新規作成",
)
def create_user(payload: UserCreate, service: UserServiceDep) -> User:
    try:
        return service.create(payload)
    except UserAlreadyExistsError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"User already exists: {exc}",
        ) from exc


@router.get("", response_model=list[User], summary="ユーザー一覧取得")
def list_users(
    service: UserServiceDep,
    limit: int = Query(default=50, ge=1, le=100),
) -> list[User]:
    return service.list(limit=limit)


@router.get("/{email}", response_model=User, summary="ユーザー詳細取得")
def get_user(email: EmailStr, service: UserServiceDep) -> User:
    try:
        return service.get(email)
    except UserNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"User not found: {exc}",
        ) from exc


@router.put("/{email}", response_model=User, summary="ユーザー更新")
def update_user(
    email: EmailStr,
    payload: UserUpdate,
    service: UserServiceDep,
) -> User:
    try:
        return service.update(email, payload)
    except UserNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"User not found: {exc}",
        ) from exc

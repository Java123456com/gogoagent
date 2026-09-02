from fastapi import Depends, Header, HTTPException
from backend.services.auth_service import AuthService

auth_service = AuthService()


def current_user(authorization: str | None = Header(default=None)) -> dict:
    if not authorization:
        raise HTTPException(401, "未登录")
    token = authorization.removeprefix("Bearer ").strip()
    user = auth_service.current_user(token)
    if not user:
        raise HTTPException(401, "登录已过期")
    return user


def current_admin(user: dict = Depends(current_user)) -> dict:
    if not user.get("admin"):
        raise HTTPException(403, "需要管理员权限")
    return user

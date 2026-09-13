"""认证接口：注册（邀请码）/ 登录 / 当前用户 / 修改密码 / 管理员用户与邀请码管理。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from auth import (create_invites, create_user, create_token, delete_invite,
                  get_current_user, get_usage, get_user, list_invites, list_users,
                  require_admin, set_user_disabled, validate_invite, verify_password)

router = APIRouter(prefix="/api/auth", tags=["auth"])


class RegisterBody(BaseModel):
    invite_code: str
    username: str
    password: str


class LoginBody(BaseModel):
    username: str
    password: str


class ChangePassBody(BaseModel):
    old_password: str
    new_password: str


class InvitesBody(BaseModel):
    count: int = 1


@router.post("/register")
def register(body: RegisterBody):
    code = (body.invite_code or "").strip().upper()
    if not validate_invite(code):
        raise HTTPException(status_code=400, detail="邀请码无效或已被使用")
    try:
        user = create_user(body.username.strip(), body.password, role="member",
                           invite_code=code)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"id": user["id"], "username": user["username"], "role": user["role"],
            "token": create_token(user["id"])}


@router.post("/login")
def login(body: LoginBody):
    user = get_user_by_username(body.username.strip())
    if not user or not verify_password(body.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    if user.get("disabled"):
        raise HTTPException(status_code=401, detail="账号已被禁用")
    return {"id": user["id"], "username": user["username"], "role": user["role"],
            "notes_root": user.get("notes_root") or "",
            "token": create_token(user["id"])}


def get_user_by_username(username: str):
    from auth import get_user_by_name
    return get_user_by_name(username)


@router.get("/me")
def me(user: dict = Depends(get_current_user)):
    u = get_user(user["id"]) or {}
    return {"id": u["id"], "username": u["username"], "role": u["role"],
            "notes_root": u.get("notes_root") or ""}


@router.post("/change-password")
def change_password(body: ChangePassBody, user: dict = Depends(get_current_user)):
    import auth as _auth
    u = get_user(user["id"])
    if not u or not verify_password(body.old_password, u["password_hash"]):
        raise HTTPException(status_code=400, detail="原密码错误")
    if len(body.new_password) < 6:
        raise HTTPException(status_code=400, detail="新密码至少 6 个字符")
    _auth.set_user_password(user["id"], body.new_password)
    return {"ok": True}


# ---------------------------------------------------------------------------
# 管理员：邀请码 / 用户 / 用量
# ---------------------------------------------------------------------------
@router.post("/invites")
def make_invites(body: InvitesBody, admin: dict = Depends(require_admin)):
    return {"codes": create_invites(admin["id"], body.count)}


@router.get("/invites")
def get_invites(admin: dict = Depends(require_admin)):
    return {"invites": list_invites()}


@router.delete("/invites/{code}")
def revoke_invite(code: str, admin: dict = Depends(require_admin)):
    if not delete_invite(code.upper()):
        raise HTTPException(status_code=404, detail="邀请码不存在或已被使用")
    return {"deleted": code.upper()}


@router.get("/users")
def get_users(admin: dict = Depends(require_admin)):
    return {"users": list_users()}


@router.post("/users/{user_id}/disable")
def disable_user(user_id: str, admin: dict = Depends(require_admin)):
    if user_id == admin["id"]:
        raise HTTPException(status_code=400, detail="不能禁用自己")
    set_user_disabled(user_id, True)
    return {"disabled": user_id}


@router.post("/users/{user_id}/enable")
def enable_user(user_id: str, admin: dict = Depends(require_admin)):
    set_user_disabled(user_id, False)
    return {"enabled": user_id}


@router.get("/usage")
def usage(days: int = 7, admin: dict = Depends(require_admin)):
    return {"usage": get_usage("", days)}

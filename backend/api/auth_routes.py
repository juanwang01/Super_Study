"""认证接口：注册（邀请码）/ 登录 / 当前用户 / 修改密码 / 管理员用户与邀请码管理。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from auth import (create_invites, create_user, create_token, delete_invite,
                  get_current_user, get_usage, get_user, get_user_by_name,
                  get_user_notes_root, list_invites, list_users, require_admin,
                  set_user_disabled, set_user_notes_root, validate_invite,
                  verify_password)

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
    user = get_user_by_name(body.username.strip())
    if not user or not verify_password(body.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    if user.get("disabled"):
        raise HTTPException(status_code=401, detail="账号已被禁用")
    return {"id": user["id"], "username": user["username"], "role": user["role"],
            "notes_root": user.get("notes_root") or "",
            "token": create_token(user["id"])}


@router.get("/me")
def me(user: dict = Depends(get_current_user)):
    u = get_user(user["id"]) or {}
    return {"id": u["id"], "username": u["username"], "role": u["role"],
            "notes_root": u.get("notes_root") or ""}


class NotesRootBody(BaseModel):
    notes_root: str = ""
    create: bool = False   # 路径不存在时，用户确认创建


@router.get("/notes-root")
def get_my_notes_root(user: dict = Depends(get_current_user)):
    return {"notes_root": get_user_notes_root(user["id"])}


@router.post("/notes-root")
def set_my_notes_root(body: NotesRootBody, user: dict = Depends(get_current_user)):
    """设置自己的学习笔记根目录（Obsidian vault 路径；空字符串=清空回到系统目录）。

    校验：必须是本地绝对路径；路径不存在时返回 need_create（前端确认后再以 create=true 提交）。
    """
    import re
    from pathlib import Path

    root = (body.notes_root or "").strip()
    if not root:
        set_user_notes_root(user["id"], "")
        return {"saved": True, "notes_root": "", "message": "已清空，笔记回到系统目录"}

    # 绝对路径校验（Windows 盘符或 UNC；预留 Linux/macOS）
    if not (re.match(r"^[A-Za-z]:[\\/]", root) or root.startswith("\\\\")
            or root.startswith("/")):
        raise HTTPException(status_code=400,
                            detail="请输入本地绝对路径，如 D:\\vault\\学习笔记（或 /home/user/notes）")

    p = Path(root)
    if not p.exists():
        if body.create:
            try:
                p.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                raise HTTPException(status_code=400, detail=f"创建目录失败：{e}")
        else:
            return {"need_create": True, "notes_root": root,
                    "message": "该目录不存在，是否自动创建？"}
    elif not p.is_dir():
        raise HTTPException(status_code=400, detail="该路径已存在，但它不是文件夹")

    set_user_notes_root(user["id"], root)
    return {"saved": True, "notes_root": root, "message": "已保存，Obsidian 可直接打开"}


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

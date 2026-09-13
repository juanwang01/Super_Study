"""用户系统：账号/邀请码/用量/认证（JWT）。

- SQLite 单库（data/system.db）：
  users   ：账号、密码哈希（pbkdf2）、角色（admin/member）、笔记目录
  invites ：一次性邀请码（admin 生成，member 注册消耗）
  usage   ：按用户按天用量（请求数/字符数）
- JWT：标准库 HMAC-SHA256 自签（零第三方依赖），密钥持久化于 .env
- 首次启动：若不存在任何用户，自动创建 admin（默认密码 admin123，请尽快修改）
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import Depends, Header, HTTPException

from config import DATA_ROOT, ENV_FILE

DB_PATH: Path = DATA_ROOT / "system.db"

# token 有效期（秒）：7 天
TOKEN_TTL = 7 * 24 * 3600

_DEFAULT_ADMIN_USER = "admin"
_DEFAULT_ADMIN_PASS = "admin123"   # 首次自动创建，请尽快在管理员页修改


# ---------------------------------------------------------------------------
# SQLite 基础
# ---------------------------------------------------------------------------
def _conn() -> sqlite3.Connection:
    DATA_ROOT.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=15)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    return c


def _init_db() -> None:
    with _conn() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'member',
                notes_root TEXT DEFAULT '',
                disabled INTEGER DEFAULT 0,
                created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS invites (
                code TEXT PRIMARY KEY,
                created_by TEXT NOT NULL,
                created_at REAL NOT NULL,
                used_by TEXT,
                used_at REAL
            );
            CREATE TABLE IF NOT EXISTS usage (
                user_id TEXT NOT NULL,
                day TEXT NOT NULL,
                requests INTEGER DEFAULT 0,
                chars INTEGER DEFAULT 0,
                PRIMARY KEY (user_id, day)
            );
            CREATE TABLE IF NOT EXISTS projects (
                project_id TEXT PRIMARY KEY,
                owner TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS llm_providers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                base_url TEXT NOT NULL,
                api_key TEXT NOT NULL,
                model TEXT NOT NULL DEFAULT '',
                models TEXT NOT NULL DEFAULT '[]',
                timeout INTEGER NOT NULL DEFAULT 180,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at REAL NOT NULL
            );
            """
        )
        # 首次启动自动创建 admin
        row = c.execute("SELECT COUNT(*) AS n FROM users").fetchone()
        if row["n"] == 0:
            c.execute(
                "INSERT INTO users (id, username, password_hash, role, created_at) VALUES (?,?,?,?,?)",
                ("admin", _DEFAULT_ADMIN_USER, hash_password(_DEFAULT_ADMIN_PASS),
                 "admin", time.time()),
            )

    # 迁移旧单配置 → 多供应商表（表空且有 .env key 时）
    try:
        from core.provider_manager import migrate_legacy_env
        migrate_legacy_env()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 密码哈希（pbkdf2，标准库）
# ---------------------------------------------------------------------------
def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", (password or "").encode("utf-8"), salt, 120_000)
    return f"pbkdf2${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, salt_hex, dk_hex = (stored or "").split("$")
        dk = hashlib.pbkdf2_hmac("sha256", (password or "").encode("utf-8"),
                                 bytes.fromhex(salt_hex), 120_000)
        return hmac.compare_digest(dk.hex(), dk_hex)
    except Exception:
        return False


# ---------------------------------------------------------------------------
# JWT（HMAC-SHA256 自签）
# ---------------------------------------------------------------------------
def _jwt_secret() -> str:
    """读取或生成 JWT 密钥（持久化在 .env，重启不失效）。"""
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("LEARN_AUTH_SECRET="):
                return line.split("=", 1)[1].strip()
    secret = secrets.token_hex(32)
    try:
        with ENV_FILE.open("a", encoding="utf-8") as f:
            f.write(f"LEARN_AUTH_SECRET={secret}\n")
    except OSError:
        pass
    return secret


def _b64e(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64d(data: str) -> bytes:
    pad = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + pad)


def create_token(user_id: str) -> str:
    header = _b64e(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    payload = _b64e(json.dumps({"uid": user_id, "exp": int(time.time()) + TOKEN_TTL}).encode())
    sig = _b64e(hmac.new(_jwt_secret().encode(), f"{header}.{payload}".encode(),
                         hashlib.sha256).digest())
    return f"{header}.{payload}.{sig}"


def _decode_token(token: str) -> str | None:
    try:
        header, payload, sig = token.split(".")
        expect = _b64e(hmac.new(_jwt_secret().encode(), f"{header}.{payload}".encode(),
                                hashlib.sha256).digest())
        if not hmac.compare_digest(sig, expect):
            return None
        data = json.loads(_b64d(payload))
        if int(data.get("exp", 0)) < time.time():
            return None
        return data.get("uid")
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 用户 CRUD
# ---------------------------------------------------------------------------
def _user_row(c: sqlite3.Connection, user_id: str) -> dict[str, Any] | None:
    r = c.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    return dict(r) if r else None


def get_user(user_id: str) -> dict[str, Any] | None:
    with _conn() as c:
        return _user_row(c, user_id)


def get_user_by_name(username: str) -> dict[str, Any] | None:
    with _conn() as c:
        r = c.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        return dict(r) if r else None


def list_users() -> list[dict[str, Any]]:
    with _conn() as c:
        rows = c.execute(
            "SELECT id, username, role, notes_root, disabled, created_at FROM users "
            "ORDER BY created_at").fetchall()
        users = [dict(r) for r in rows]
        for u in users:
            n = c.execute(
                "SELECT COUNT(*) AS n FROM projects WHERE owner=?", (u["id"],)).fetchone()
            u["project_count"] = n["n"] if n else 0
        return users


def create_user(username: str, password: str, role: str = "member",
                invite_code: str = "") -> dict[str, Any]:
    if not username or len(username) < 2:
        raise ValueError("用户名至少 2 个字符")
    if not password or len(password) < 6:
        raise ValueError("密码至少 6 个字符")
    with _conn() as c:
        if c.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
            raise ValueError("用户名已存在")
        uid = secrets.token_hex(8)
        c.execute(
            "INSERT INTO users (id, username, password_hash, role, notes_root, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (uid, username, hash_password(password), role, "", time.time()))
        if invite_code:
            c.execute("UPDATE invites SET used_by=?, used_at=? WHERE code=?",
                      (uid, time.time(), invite_code))
        return {"id": uid, "username": username, "role": role}


def set_user_disabled(user_id: str, disabled: bool) -> None:
    with _conn() as c:
        c.execute("UPDATE users SET disabled=? WHERE id=?", (1 if disabled else 0, user_id))


def set_user_password(user_id: str, new_password: str) -> None:
    if len(new_password or "") < 6:
        raise ValueError("密码至少 6 个字符")
    with _conn() as c:
        c.execute("UPDATE users SET password_hash=? WHERE id=?",
                  (hash_password(new_password), user_id))


def set_user_notes_root(user_id: str, notes_root: str) -> None:
    """设置用户的学习笔记根目录（Obsidian vault 路径，空字符串=清空）。"""
    with _conn() as c:
        c.execute("UPDATE users SET notes_root=? WHERE id=?", (notes_root or "", user_id))


def get_user_notes_root(user_id: str) -> str:
    """返回用户配置的笔记根目录；未配置返回空串（由 pm 回退全局配置）。"""
    with _conn() as c:
        row = c.execute("SELECT notes_root FROM users WHERE id=?", (user_id,)).fetchone()
    return (row[0] or "") if row else ""


def set_user_notes_root(user_id: str, notes_root: str) -> None:
    with _conn() as c:
        c.execute("UPDATE users SET notes_root=? WHERE id=?", (notes_root.strip(), user_id))


# ---------------------------------------------------------------------------
# 邀请码
# ---------------------------------------------------------------------------
def create_invites(created_by: str, count: int = 1) -> list[str]:
    count = max(1, min(int(count or 1), 50))
    codes: list[str] = []
    with _conn() as c:
        for _ in range(count):
            code = secrets.token_hex(4).upper()   # 8 位，如 A1B2C3D4
            c.execute("INSERT INTO invites (code, created_by, created_at) VALUES (?,?,?)",
                      (code, created_by, time.time()))
            codes.append(code)
    return codes


def list_invites() -> list[dict[str, Any]]:
    with _conn() as c:
        rows = c.execute("SELECT * FROM invites ORDER BY created_at DESC").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["used"] = bool(d.get("used_by"))
            out.append(d)
        return out


def delete_invite(code: str) -> bool:
    with _conn() as c:
        cur = c.execute("DELETE FROM invites WHERE code=? AND used_by IS NULL", (code,))
        return cur.rowcount > 0


def validate_invite(code: str) -> bool:
    with _conn() as c:
        r = c.execute("SELECT 1 FROM invites WHERE code=? AND used_by IS NULL", (code,)).fetchone()
        return r is not None


# ---------------------------------------------------------------------------
# 用量统计
# ---------------------------------------------------------------------------
def record_usage(user_id: str, chars: int = 0) -> None:
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with _conn() as c:
        c.execute(
            "INSERT INTO usage (user_id, day, requests, chars) VALUES (?,?,1,?) "
            "ON CONFLICT(user_id, day) DO UPDATE SET "
            "requests = requests + 1, chars = chars + excluded.chars",
            (user_id, day, chars))


def get_usage(user_id: str = "", days: int = 7) -> list[dict[str, Any]]:
    with _conn() as c:
        if user_id:
            rows = c.execute(
                "SELECT user_id, day, requests, chars FROM usage WHERE user_id=? "
                "ORDER BY day DESC LIMIT ?", (user_id, days)).fetchall()
        else:
            rows = c.execute(
                "SELECT user_id, day, requests, chars FROM usage ORDER BY day DESC LIMIT ?",
                (days,)).fetchall()
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# FastAPI 认证依赖
# ---------------------------------------------------------------------------
def get_current_user(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="未登录")
    user_id = _decode_token(authorization.split(" ", 1)[1].strip())
    if not user_id:
        raise HTTPException(status_code=401, detail="登录已过期，请重新登录")
    user = get_user(user_id)
    if not user or user.get("disabled"):
        raise HTTPException(status_code=401, detail="账号不存在或已被禁用")
    return user


def require_admin(user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user


# 模块加载末尾初始化数据库（此时所有函数已定义）
_init_db()

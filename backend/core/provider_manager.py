"""LLM 供应商（多 Key 池）管理。

数据结构（SQLite 表 llm_providers）：
- id           自增主键
- name         显示名称（如 DeepSeek / 硅基流动）
- base_url     OpenAI 兼容接口地址
- api_key      API Key（明文仅存服务端，绝不下发前端）
- model        该供应商默认/常用模型
- models       支持的模型列表（JSON 数组）
- timeout      超时秒数
- enabled      是否对普通用户开放（0/1）
- created_at

普通用户只拿到「供应商名 + 模型名」下拉选项，永远看不到 Key。
"""
from __future__ import annotations

import json
import time
from typing import Any

from auth import _conn


def _row_to_dict(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "name": row["name"],
        "base_url": row["base_url"],
        "api_key": row["api_key"],
        "model": row["model"],
        "models": json.loads(row["models"] or "[]"),
        "timeout": row["timeout"],
        "enabled": bool(row["enabled"]),
        "created_at": row["created_at"],
    }


def list_providers(include_disabled: bool = True) -> list[dict[str, Any]]:
    """管理端：全部供应商（含禁用）。返回带明文 key——仅供管理后台内部使用。"""
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM llm_providers ORDER BY id"
        ).fetchall() if include_disabled else c.execute(
            "SELECT * FROM llm_providers WHERE enabled=1 ORDER BY id"
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def get_provider(provider_id: int) -> dict[str, Any] | None:
    with _conn() as c:
        row = c.execute("SELECT * FROM llm_providers WHERE id=?", (provider_id,)).fetchone()
    return _row_to_dict(row) if row else None


def first_enabled() -> dict[str, Any] | None:
    """第一个启用的供应商（会话未指定模型时的兜底）。"""
    with _conn() as c:
        row = c.execute(
            "SELECT * FROM llm_providers WHERE enabled=1 ORDER BY id LIMIT 1"
        ).fetchone()
    return _row_to_dict(row) if row else None


def mask_key(key: str) -> str:
    if not key:
        return ""
    if len(key) <= 8:
        return "****"
    return key[:2] + "****" + key[-4:]


def add_provider(name: str, base_url: str, api_key: str, model: str,
                 models: list[str] | None = None, timeout: int = 180) -> dict[str, Any]:
    with _conn() as c:
        cur = c.execute(
            """INSERT INTO llm_providers
               (name, base_url, api_key, model, models, timeout, enabled, created_at)
               VALUES (?,?,?,?,?,?,1,?)""",
            (name.strip(), base_url.strip(), api_key.strip(), model.strip(),
             json.dumps(models or [], ensure_ascii=False), int(timeout), time.time()),
        )
        pid = cur.lastrowid
    return get_provider(pid)


def update_provider(provider_id: int, *, name: str | None = None,
                    base_url: str | None = None, api_key: str | None = None,
                    model: str | None = None, models: list[str] | None = None,
                    timeout: int | None = None, enabled: bool | None = None) -> dict[str, Any] | None:
    """修改供应商。api_key 为 None/空串 = 不修改。models 传 None = 不修改。"""
    cur = get_provider(provider_id)
    if cur is None:
        return None
    sets: list[str] = []
    args: list[Any] = []
    if name is not None:
        sets.append("name=?"); args.append(name.strip())
    if base_url is not None:
        sets.append("base_url=?"); args.append(base_url.strip())
    if api_key:
        sets.append("api_key=?"); args.append(api_key.strip())
    if model is not None:
        sets.append("model=?"); args.append(model.strip())
    if models is not None:
        sets.append("models=?"); args.append(json.dumps(models, ensure_ascii=False))
    if timeout is not None:
        sets.append("timeout=?"); args.append(int(timeout))
    if enabled is not None:
        sets.append("enabled=?"); args.append(1 if enabled else 0)
    if not sets:
        return cur
    args.append(provider_id)
    with _conn() as c:
        c.execute(f"UPDATE llm_providers SET {', '.join(sets)} WHERE id=?", args)
    return get_provider(provider_id)


def delete_provider(provider_id: int) -> bool:
    with _conn() as c:
        cur = c.execute("DELETE FROM llm_providers WHERE id=?", (provider_id,))
        return cur.rowcount > 0


def migrate_legacy_env() -> None:
    """表为空且 .env 存在旧单配置时，迁移为第一个供应商。"""
    from config import get_llm_config
    with _conn() as c:
        n = c.execute("SELECT COUNT(*) AS n FROM llm_providers").fetchone()["n"]
        if n > 0:
            return
    cfg = get_llm_config()
    if cfg.get("api_key"):
        base = (cfg["base_url"] or "").rstrip("/")
        name = "DeepSeek" if "deepseek" in base else "自定义服务"
        try:
            add_provider(name, base, cfg["api_key"], cfg.get("model") or "",
                         timeout=int(cfg.get("timeout") or 180))
        except Exception:
            pass

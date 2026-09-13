"""项目内会话（线程）管理：同一课程可开多个会话，共享项目快照/素材/计划。

轻量设计：
- 会话历史按项目隔离，存于 data/{id}/.learn_config/threads/{thread_id}.json
- 切换会话 = 换一批对话历史；AI 上下文由当前会话历史 + 项目状态快照构成
- 会话删除不可恢复（仅删对话历史，不影响笔记/快照/素材）
"""
from __future__ import annotations

import json
import re
import time
import uuid
from pathlib import Path
from typing import Any

from core import project_manager as pm

THREADS_DIR = "threads"
MAX_MESSAGES_PER_THREAD = 200


def _threads_dir(project_id: str) -> Path:
    pdir = pm._project_dir(project_id)
    d = pdir / pm.CONFIG_DIR_NAME / THREADS_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def _thread_file(project_id: str, thread_id: str) -> Path:
    return _threads_dir(project_id) / f"{thread_id}.json"


def _safe_name(name: str) -> str:
    name = (name or "").strip() or "未命名会话"
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)[:30]
    return name or "未命名会话"


def _read(project_id: str, thread_id: str) -> dict[str, Any] | None:
    f = _thread_file(project_id, thread_id)
    if not f.exists():
        return None
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return None


def _write(project_id: str, thread_id: str, data: dict[str, Any]) -> None:
    f = _thread_file(project_id, thread_id)
    f.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def list_threads(project_id: str) -> list[dict[str, Any]]:
    """列出项目内全部会话（按更新时间倒序）。"""
    d = _threads_dir(project_id)
    out = []
    for f in d.glob("*.json"):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        out.append({
            "thread_id": data.get("thread_id", f.stem),
            "name": data.get("name", "未命名会话"),
            "message_count": len(data.get("messages", [])),
            "updated_at": data.get("updated_at", 0),
            "created_at": data.get("created_at", 0),
        })
    out.sort(key=lambda t: t.get("updated_at") or 0, reverse=True)
    return out


def create_thread(project_id: str, name: str = "") -> dict[str, Any]:
    """新建会话；若项目暂无会话，首个命名为「默认会话」。"""
    existing = list_threads(project_id)
    tid = uuid.uuid4().hex[:12]
    final_name = _safe_name(name) if name else (f"会话 {len(existing) + 1}" if existing else "默认会话")
    now = time.time()
    data = {
        "thread_id": tid, "name": final_name,
        "messages": [], "created_at": now, "updated_at": now,
    }
    _write(project_id, tid, data)
    return {"thread_id": tid, "name": final_name, "message_count": 0,
            "created_at": now, "updated_at": now}


def latest_thread(project_id: str) -> dict[str, Any] | None:
    lst = list_threads(project_id)
    return lst[0] if lst else None


def get_thread(project_id: str, thread_id: str) -> dict[str, Any] | None:
    data = _read(project_id, thread_id)
    if not data:
        return None
    return {
        "thread_id": data["thread_id"], "name": data.get("name", ""),
        "message_count": len(data.get("messages", [])),
        "updated_at": data.get("updated_at", 0),
    }


def _clean_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """清洗会话历史：只保留 user/assistant 纯文本消息，剥离 tool_calls。

    DeepSeek 要求带 tool_calls 的 assistant 消息必须紧跟 tool 响应；
    历史仅存对话文本，避免加载后下次请求 400。
    """
    out: list[dict[str, Any]] = []
    for m in messages or []:
        role = m.get("role")
        if role == "assistant":
            out.append({k: v for k, v in m.items() if k != "tool_calls"})
        elif role == "user":
            out.append(m)
    return out


def load_messages(project_id: str, thread_id: str) -> list[dict[str, Any]]:
    data = _read(project_id, thread_id)
    if not data:
        return []
    return _clean_messages(data.get("messages", []))


def save_messages(project_id: str, thread_id: str, messages: list[dict[str, Any]]) -> None:
    """持久化会话历史（清洗后保留最近 MAX_MESSAGES_PER_THREAD 条）。"""
    data = _read(project_id, thread_id)
    if data is None:
        data = {"thread_id": thread_id, "name": "未命名会话",
                "created_at": time.time()}
    data["messages"] = _clean_messages(messages)[-MAX_MESSAGES_PER_THREAD:]
    data["updated_at"] = time.time()
    _write(project_id, thread_id, data)


def get_summary(project_id: str, thread_id: str) -> str:
    """读取会话压缩摘要（无则空串）。"""
    data = _read(project_id, thread_id)
    if not data:
        return ""
    return data.get("summary", "") or ""


def set_summary(project_id: str, thread_id: str, summary: str) -> None:
    """写入会话摘要（保留 messages 与元信息）。"""
    data = _read(project_id, thread_id)
    if data is None:
        data = {"thread_id": thread_id, "name": "未命名会话",
                "created_at": time.time(), "messages": []}
    data["summary"] = (summary or "").strip()
    data["updated_at"] = time.time()
    _write(project_id, thread_id, data)


def rename_thread(project_id: str, thread_id: str, name: str) -> dict[str, Any]:
    data = _read(project_id, thread_id)
    if data is None:
        raise FileNotFoundError("会话不存在")
    data["name"] = _safe_name(name)
    _write(project_id, thread_id, data)
    return {"thread_id": thread_id, "name": data["name"]}


def delete_thread(project_id: str, thread_id: str) -> dict[str, Any]:
    f = _thread_file(project_id, thread_id)
    if not f.exists():
        raise FileNotFoundError("会话不存在")
    f.unlink()
    return {"deleted": thread_id}

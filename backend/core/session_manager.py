"""会话管理：session 与学习项目的绑定、切换、状态隔离。

规则（对应 skill 硬性约束 9）：
- 切换项目 / 返回主菜单前，必须先保存当前项目快照；
- 加载新项目时，从磁盘快照恢复 learner_state 到内存；
- 一个 session 同时只绑定一个项目；learner_state 保存在磁盘快照（权威），
  内存仅保留会话级上下文。
"""
from __future__ import annotations

import threading
import time
import uuid
from typing import Any

from core import project_manager as pm
from core import project_threads as pt


class Session:
    def __init__(self, session_id: str, owner: str = "") -> None:
        self.session_id = session_id
        self.owner = owner                        # 会话归属用户 id（数据隔离）
        self.project_id: str | None = None          # 当前绑定的项目
        self.project_name: str = ""
        self.mode: str = "general"                  # document_anchor | general
        self.learner_state: dict[str, Any] = {}     # 内存学习者状态（快照为权威）
        self.snapshot_mtime: float | None = None    # 加载快照时的磁盘 mtime（并发检测）
        self.messages: list[dict[str, Any]] = []    # 本会话与 LLM 的对话历史
        self.current_thread: str | None = None      # 项目内会话（线程）id
        self.summary: str = ""                      # 当前线程的压缩摘要（历史上下文）
        self.model_provider: int | None = None      # 当前使用的 LLM 供应商 id（None=系统默认）
        self.created_at: float = time.time()
        self.updated_at: float = time.time()
        self._lock = threading.Lock()

    def touch(self) -> None:
        self.updated_at = time.time()

    def is_idle(self, idle_seconds: int) -> bool:
        return time.time() - self.updated_at > idle_seconds


class SessionManager:
    def __init__(self, idle_seconds: int = 900) -> None:
        self._sessions: dict[str, Session] = {}
        self._lock = threading.Lock()
        self._idle_seconds = idle_seconds

    def create_session(self, user_id: str = "") -> Session:
        sid = uuid.uuid4().hex
        s = Session(sid, owner=user_id)
        with self._lock:
            self._sessions[sid] = s
        return s

    def get_session(self, session_id: str) -> Session | None:
        with self._lock:
            return self._sessions.get(session_id)

    def open_project(self, session: Session, project_id: str) -> dict[str, Any]:
        """绑定项目：先保存旧项目快照与会话历史，再加载新项目上下文。"""
        with session._lock:
            # 1) 若当前已绑定其他项目，先保存快照 + 会话历史
            if session.project_id and session.project_id != project_id:
                self._persist_thread(session)
                pm.save_snapshot(session.project_id, session.learner_state,
                                 expected_mtime=session.snapshot_mtime)

            # 2) 加载新项目
            ctx = pm.load_project(project_id)
            session.project_id = project_id
            session.project_name = ctx["meta"]["project_name"]
            session.mode = ctx["meta"]["mode"]
            session.learner_state = ctx["learner_state"]
            session.snapshot_mtime = _snapshot_file_mtime(project_id)

            # 3) 项目内会话：恢复最近会话，否则新建「默认会话」
            thread = pt.latest_thread(project_id)
            if thread is None:
                thread = pt.create_thread(project_id, "默认会话")
            session.current_thread = thread["thread_id"]
            session.messages = pt.load_messages(project_id, thread["thread_id"])
            session.summary = pt.get_summary(project_id, thread["thread_id"])
            session.touch()

        return {
            "project_id": project_id,
            "project_name": session.project_name,
            "mode": session.mode,
            "meta": ctx["meta"],
            "learner_state": session.learner_state,
            "plan_latest": ctx["plan_latest"],
            "notes": ctx["notes"],
            "materials": ctx["materials"],
            "thread_id": session.current_thread,
            "thread_name": thread["name"],
            "threads": pt.list_threads(project_id),
            "model_provider": session.model_provider,
        }

    def _persist_thread(self, session: Session) -> None:
        """保存当前会话历史到项目线程文件。"""
        try:
            if session.project_id and session.current_thread:
                pt.save_messages(session.project_id, session.current_thread,
                                 session.messages)
        except Exception:
            pass

    def _save_snapshot(self, session: Session) -> dict[str, Any]:
        """保存快照并刷新 snapshot_mtime，避免误报"另一个会话编辑"。

        每次成功写盘后，把磁盘最新 mtime 记回 session，下次保存用它做
        并发检测基准；否则 60 秒自动保存会一直拿初始 mtime 对比，
        永远误报并发编辑。
        """
        result = pm.save_snapshot(session.project_id, session.learner_state,
                                  expected_mtime=session.snapshot_mtime)
        mtime = result.get("latest_mtime")
        if mtime:
            session.snapshot_mtime = mtime
        return result

    def switch_thread(self, session: Session, thread_id: str) -> dict[str, Any]:
        """切换项目内会话：先保存当前历史，再加载目标会话历史。"""
        with session._lock:
            if not session.project_id:
                raise ValueError("未绑定项目，无法切换会话")
            target = pt.get_thread(session.project_id, thread_id)
            if target is None:
                raise FileNotFoundError("会话不存在")
            self._persist_thread(session)
            session.current_thread = thread_id
            session.messages = pt.load_messages(session.project_id, thread_id)
            session.summary = pt.get_summary(session.project_id, thread_id)
            session.touch()
        return {
            "thread_id": thread_id,
            "thread_name": target["name"],
            "message_count": target["message_count"],
            "threads": pt.list_threads(session.project_id),
        }

    def new_thread(self, session: Session, name: str = "") -> dict[str, Any]:
        """新建项目内会话并激活。"""
        with session._lock:
            if not session.project_id:
                raise ValueError("未绑定项目，无法新建会话")
            self._persist_thread(session)
            thread = pt.create_thread(session.project_id, name)
            session.current_thread = thread["thread_id"]
            session.messages = []
            session.summary = ""
            session.touch()
        return {**thread, "threads": pt.list_threads(session.project_id)}

    def close_project(self, session: Session) -> dict[str, Any]:
        """保存当前项目快照与会话历史，解除绑定，回到全局主菜单。"""
        with session._lock:
            result: dict[str, Any] = {"saved": None}
            if session.project_id:
                self._persist_thread(session)
                result["saved"] = self._save_snapshot(session)
                session.project_id = None
                session.project_name = ""
                session.learner_state = {}
                session.snapshot_mtime = None
                session.messages = []
                session.summary = ""
                session.current_thread = None
            session.touch()
        return result

    def update_learner_state(self, session: Session, learner_state: dict[str, Any]) -> None:
        with session._lock:
            session.learner_state = dict(learner_state or {})
            session.touch()

    def persist_current(self, session: Session) -> dict[str, Any]:
        """强制保存当前项目快照（不解除绑定）。"""
        with session._lock:
            if not session.project_id:
                return {"saved": False, "reason": "未绑定项目"}
            result = self._save_snapshot(session)
            session.touch()
            return {"saved": True, **result}

    def sweep_idle(self) -> int:
        """闲置会话保底保存快照；【绝不删除会话】——会话只能由用户手动删除。"""
        now = time.time()
        removed = 0
        with self._lock:
            for sid in list(self._sessions.keys()):
                s = self._sessions[sid]
                if s.project_id and now - s.updated_at > self._idle_seconds:
                    self._persist_thread(s)
                    try:
                        self._save_snapshot(s)
                    except Exception:
                        pass
        return removed


def _snapshot_file_mtime(project_id: str) -> float | None:
    try:
        pdir = pm._project_dir(project_id)
        f = pdir / pm.CONFIG_DIR_NAME / pm.SNAPSHOT_DIR / pm.SNAPSHOT_LATEST
        return f.stat().st_mtime if f.exists() else None
    except Exception:
        return None


# 全局单例：API 层与 Agent 工具执行器共享
session_manager = SessionManager()

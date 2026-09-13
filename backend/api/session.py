"""会话 API：创建会话 / 打开关闭项目 / 发送消息 / 强制保存。"""
from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from auth import get_current_user
from core import project_manager as pm
from core import project_threads as pt
from core.agent_bridge import run_conversation
from core.session_manager import session_manager

router = APIRouter(prefix="/api/session", tags=["session"])

@router.get("/llm/options")
def llm_options(user: dict = Depends(get_current_user)):
    """普通用户可见的模型选项：只含启用供应商的「供应商名 + 模型」，绝不含 Key。"""
    from core import provider_manager as prov
    options: list[dict] = []
    for p in prov.list_providers(include_disabled=False):
        if not (p["model"] or "").strip():
            continue
        options.append({
            "provider_id": p["id"],
            "provider_name": p["name"],
            "model": p["model"],
            "models": p["models"],
        })
    return {"options": options}

def _session_owner(session_id: str, user: dict):
    """会话归属校验：只能操作自己的会话。"""
    s = session_manager.get_session(session_id)
    if s is None:
        raise HTTPException(status_code=404, detail="会话不存在或已过期")
    if s.owner and s.owner != user.get("id") and user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="无权操作该会话")
    return s


def _check_project_owner(project_id: str, user: dict) -> None:
    owner = pm._owner_of(project_id)
    if user.get("role") != "admin" and owner != user.get("id"):
        raise HTTPException(status_code=403, detail="无权访问该项目")



class OpenProjectBody(BaseModel):
    project_id: str


class MessageBody(BaseModel):
    content: str


class ThreadNameBody(BaseModel):
    name: str


class ModelBody(BaseModel):
    provider_id: int | None = None   # None = 回系统默认


@router.post("")
def create_session(user: dict = Depends(get_current_user)):
    s = session_manager.create_session(user_id=user.get("id"))
    return {"session_id": s.session_id, "projects": pm.list_projects(owner=user.get("id"))}


@router.get("/{session_id}")
def get_session(session_id: str, user: dict = Depends(get_current_user)):
    s = _session_owner(session_id, user)
    s = session_manager.get_session(session_id)
    if not s:
        raise HTTPException(status_code=404, detail="会话不存在或已过期")
    return {
        "session_id": s.session_id,
        "project_id": s.project_id,
        "project_name": s.project_name,
        "mode": s.mode,
    }


@router.post("/{session_id}/open")
def open_project(session_id: str, body: OpenProjectBody, user: dict = Depends(get_current_user)):
    s = _session_owner(session_id, user)
    _check_project_owner(body.project_id, user)
    s = session_manager.get_session(session_id)
    if not s:
        raise HTTPException(status_code=404, detail="会话不存在或已过期")
    try:
        ctx = session_manager.open_project(s, body.project_id)
    except (ValueError, FileNotFoundError) as e:
        raise HTTPException(status_code=404, detail=str(e))
    ctx["guide"] = build_project_guide(ctx, is_new=False)
    return ctx


@router.put("/{session_id}/model")
def set_session_model(session_id: str, body: ModelBody,
                      user: dict = Depends(get_current_user)):
    """切换当前会话使用的 LLM 模型（用户侧：只传供应商 id，永远接触不到 Key）。"""
    s = _session_owner(session_id, user)
    from core import provider_manager as prov
    if body.provider_id is not None:
        p = prov.get_provider(body.provider_id)
        if p is None:
            raise HTTPException(status_code=404, detail="模型不存在")
        if not p["enabled"]:
            raise HTTPException(status_code=403, detail="该模型已被管理员停用")
    s.model_provider = body.provider_id
    s.touch()
    return {"provider_id": s.model_provider}


@router.post("/{session_id}/close")
def close_project(session_id: str, user: dict = Depends(get_current_user)):
    s = _session_owner(session_id, user)
    s = session_manager.get_session(session_id)
    if not s:
        raise HTTPException(status_code=404, detail="会话不存在或已过期")
    return session_manager.close_project(s)


@router.post("/{session_id}/persist")
def persist(session_id: str, user: dict = Depends(get_current_user)):
    s = _session_owner(session_id, user)
    s = session_manager.get_session(session_id)
    if not s:
        raise HTTPException(status_code=404, detail="会话不存在或已过期")
    return session_manager.persist_current(s)


# ---------------------------------------------------------------------------
# 项目内会话（线程）管理：同一课程多个会话
# ---------------------------------------------------------------------------
@router.get("/{session_id}/messages")
def get_messages(session_id: str, user: dict = Depends(get_current_user)):
    """读取当前会话的对话历史（用于前端切换会话后渲染）。"""
    s = _session_owner(session_id, user)
    s = session_manager.get_session(session_id)
    if not s:
        raise HTTPException(status_code=404, detail="会话不存在或已过期")
    return {"messages": s.messages[-80:]}


@router.get("/{session_id}/threads")
def list_threads(session_id: str, user: dict = Depends(get_current_user)):
    s = _session_owner(session_id, user)
    s = session_manager.get_session(session_id)
    if not s:
        raise HTTPException(status_code=404, detail="会话不存在或已过期")
    if not s.project_id:
        return {"threads": [], "thread_id": None, "thread_name": ""}
    return {"threads": pt.list_threads(s.project_id),
            "thread_id": s.current_thread,
            "thread_name": pt.get_thread(s.project_id, s.current_thread)["name"]
            if s.current_thread and pt.get_thread(s.project_id, s.current_thread) else ""}


@router.post("/{session_id}/threads")
def create_thread(session_id: str, body: ThreadNameBody, user: dict = Depends(get_current_user)):
    s = _session_owner(session_id, user)
    s = session_manager.get_session(session_id)
    if not s:
        raise HTTPException(status_code=404, detail="会话不存在或已过期")
    try:
        return session_manager.new_thread(s, body.name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/{session_id}/threads/{thread_id}/activate")
def activate_thread(session_id: str, thread_id: str, user: dict = Depends(get_current_user)):
    s = _session_owner(session_id, user)
    s = session_manager.get_session(session_id)
    if not s:
        raise HTTPException(status_code=404, detail="会话不存在或已过期")
    try:
        return session_manager.switch_thread(s, thread_id)
    except (ValueError, FileNotFoundError) as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/{session_id}/threads/{thread_id}/compress")
def compress_thread_endpoint(session_id: str, thread_id: str,
                             user: dict = Depends(get_current_user)):
    """手动压缩当前会话上下文（旧消息→摘要，只保留最近消息）。"""
    s = _session_owner(session_id, user)
    s = session_manager.get_session(session_id)
    if not s:
        raise HTTPException(status_code=404, detail="会话不存在或已过期")
    if not s.project_id:
        raise HTTPException(status_code=400, detail="未绑定项目")
    if thread_id != s.current_thread:
        # 允许压缩任意自己的会话：临时切换目标线程再压缩
        raise HTTPException(status_code=400, detail="请先切换到该会话再压缩")
    from core.agent_bridge import compress_thread
    return compress_thread(s)


@router.post("/{session_id}/threads/{thread_id}/rename")
def rename_thread(session_id: str, thread_id: str, body: ThreadNameBody):
    s = session_manager.get_session(session_id)
    if not s:
        raise HTTPException(status_code=404, detail="会话不存在或已过期")
    if not s.project_id:
        raise HTTPException(status_code=400, detail="未绑定项目")
    try:
        return pt.rename_thread(s.project_id, thread_id, body.name)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.delete("/{session_id}/threads/{thread_id}")
def delete_thread(session_id: str, thread_id: str, user: dict = Depends(get_current_user)):
    s = _session_owner(session_id, user)
    s = session_manager.get_session(session_id)
    if not s:
        raise HTTPException(status_code=404, detail="会话不存在或已过期")
    if not s.project_id:
        raise HTTPException(status_code=400, detail="未绑定项目")
    if thread_id == s.current_thread:
        raise HTTPException(status_code=400, detail="不能删除当前正在使用的会话，请先切换到其他会话")
    try:
        return pt.delete_thread(s.project_id, thread_id)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/{session_id}/message")
def send_message(session_id: str, body: MessageBody, user: dict = Depends(get_current_user)):
    """发送用户消息。

    结构指令（不经过 LLM，由后端直接处理）：
    - 未绑定项目时："new <主题>" / "<数字>" / 其他文本 → 新建或打开项目
    - 绑定项目时："main" / "reload" / "plan" 等 → 后端处理
    其余内容进入 LLM（探查/规划/教学/诊断）。
    """
    s = _session_owner(session_id, user)
    s = session_manager.get_session(session_id)
    if not s:
        raise HTTPException(status_code=404, detail="会话不存在或已过期")

    content = (body.content or "").strip()
    if not content:
        raise HTTPException(status_code=400, detail="消息内容不能为空")

    # ---------- 结构指令处理 ----------
    if s.project_id is None:
        # 全局主菜单状态
        if re.fullmatch(r"\d+", content):
            projects = pm.list_projects(owner=user.get("id"))
            idx = int(content) - 1
            if not (0 <= idx < len(projects)):
                raise HTTPException(status_code=400, detail="编号超出范围")
            ctx = session_manager.open_project(s, projects[idx]["project_id"])
            return {"handled": "open_project", **ctx, "reply": None}

        if content.lower().startswith("new ") or content.lower().startswith("new，"):
            name = content[4:].strip()
            info = pm.create_project(name, owner=user.get("id"))
            ctx = session_manager.open_project(s, info["project_id"])
            ctx["guide"] = build_project_guide(ctx, is_new=True)
            return {"handled": "create_project", **ctx, "reply": None}

        # 其他文本在未绑定项目时 → 当作新建项目主题
        if content not in ("help", "exit"):
            info = pm.create_project(content, owner=user.get("id"))
            ctx = session_manager.open_project(s, info["project_id"])
            ctx["guide"] = build_project_guide(ctx, is_new=True)
            return {"handled": "create_project", **ctx, "reply": None}
    else:
        # 项目内状态
        lower = content.lower()
        if lower in ("main", "切换主题", "switch topic"):
            result = session_manager.close_project(s)
            return {"handled": "main", "projects": pm.list_projects(), **result,
                    "reply": None}
        if lower in ("reload",):
            ctx = pm.reload_project(s.project_id)
            s.learner_state = ctx["learner_state"]
            s.touch()
            return {"handled": "reload", "reply": "已重新从磁盘加载项目素材、计划与元数据。"}
        if lower in ("plan",):
            ctx = pm.load_project(s.project_id)
            return {"handled": "plan", "reply": ctx["plan_latest"]}
        if lower in ("progress", "查看进度", "show my progress"):
            return {"handled": "progress",
                    "reply": _format_progress(s.learner_state)}
        if lower in ("导出日志", "export log"):
            return {"handled": "log", "reply": pm.read_log(s.project_id)}

    # ---------- 进入 LLM ----------
    try:
        result = run_conversation(s, content)
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))
    if "error" in result:
        raise HTTPException(status_code=502, detail=result["error"])
    # 对话完成后持久化到项目内会话
    session_manager._persist_thread(s)
    return {"handled": "llm", "reply": result["reply"], "tool_events": result.get("tool_events", [])}


def build_project_guide(ctx: dict, is_new: bool = False) -> str:
    """进入项目时自动生成的结构化引导（不走 LLM，即时返回）。

    引导分析：项目状态摘要、运行模式判定、资料准备询问、下一步行动。
    """
    meta = ctx["meta"]
    materials = ctx.get("materials") or []
    notes = ctx.get("notes") or []
    plan = (ctx.get("plan_latest") or "").strip()
    state = ctx.get("learner_state") or {}
    mode = meta.get("mode", "general")

    lines = [f"===== 进入项目【{meta.get('project_name', '')}】====="]

    # 1) 磁盘同步状态（一行）
    mat_desc = f"素材 {len(materials)} 份" if materials else "无素材"
    plan_desc = "有计划" if plan else "无计划"
    state_desc = "有进度" if (state.get("mastered") or state.get("current_teaching_unit")) else "进度空白"
    lines.append(f"状态：{mat_desc} · {plan_desc} · 笔记 {len(notes)} · {state_desc}")

    # 2) 模式判定（资料需求由前端 smartMaterialCheck 智能判断，不在此处询问）
    if mode == "general":
        lines.append("")
        lines.append("当前为【通用学习模式】（无本地素材）。教学会基于内置知识讲解。")
    else:
        lines.append("")
        lines.append(f"✅ 文档锚定模式：教学严格引用素材区原文（{len(materials)} 份）。")

    # 3) 下一步行动：先进入 AI 准备分析阶段，再开始学习流程
    lines.append("")
    if state.get("current_teaching_unit"):
        lines.append(f"📖 上次学到：{state['current_teaching_unit']}。")
    lines.append("🧠 正在分析学习准备（状态/资料缺口）…")

    return "\n".join(lines)


def _format_progress(state: dict) -> str:
    if not state:
        return "（暂无学习者状态）"
    lines = [
        f"- 已掌握：{', '.join(state.get('mastered') or []) or '无'}",
        f"- 部分理解：{', '.join((x.get('concept', '') for x in state.get('partial_understand') or [])) or '无'}",
        f"- 认知误区：{', '.join((x.get('concept', '') for x in state.get('misconceptions') or [])) or '无'}",
        f"- 缺失前置：{', '.join(state.get('missing_prerequisite') or []) or '无'}",
        f"- ZPD 状态：{state.get('zpd_status', 'within')}",
        f"- 当前单元：{state.get('current_teaching_unit') or '未开始'}",
        f"- 待讲清单：{', '.join(state.get('pending_list') or []) or '无'}",
    ]
    return "**当前学习进度**\n" + "\n".join(lines)

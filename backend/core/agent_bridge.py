"""Agent 调度模块：
1. 读取 skill.md 组装 system prompt；
2. 定义 LLM function-call 工具集；
3. 调用 OpenAI 兼容 /chat/completions，循环处理 tool_calls 直到模型给出最终回复。
"""
from __future__ import annotations

import json
from typing import Any, Callable

import httpx

from config import SKILL_PATH, get_http_proxy, get_llm_config
from core import project_manager as pm
from core import project_threads as pt
from core.session_manager import Session


# ---------------------------------------------------------------------------
# 工具定义（OpenAI function calling schema）
# ---------------------------------------------------------------------------
def _tool(name: str, description: str, params: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": params,
                "required": required or [],
            },
        },
    }


TOOLS: list[dict[str, Any]] = [
    _tool(
        "list_project",
        "获取全部学习项目列表，返回 id、名称、模式、最后学习时间、笔记数量。",
        {},
    ),
    _tool(
        "create_project",
        "创建全新学习项目（生成完整目录结构）。",
        {"project_name": {"type": "string", "description": "项目名称/主题名"},
         "description": {"type": "string", "description": "项目描述（可选）"}},
        ["project_name"],
    ),
    _tool(
        "open_project",
        "打开指定学习项目：加载其状态快照与最新计划，进入该项目（会先自动保存当前项目快照）。",
        {"project_id": {"type": "string", "description": "项目 id"}},
        ["project_id"],
    ),
    _tool(
        "save_snapshot",
        "把当前学习者状态（已掌握/部分理解/误区/缺失前置/ZPD 状态/待讲清单等）持久化为状态快照。",
        {"learner_state": {"type": "object", "description": "学习者状态对象，字段见 skill 的 learner_state 结构"}},
        ["learner_state"],
    ),
    _tool(
        "plan_write",
        "保存学习计划为 markdown，生成历史版本并更新 plan_latest.md。计划发生任何变更（跳步/插入前置/删减）都必须调用。",
        {"plan_markdown": {"type": "string", "description": "完整学习计划 markdown 文本"},
         "change_reason": {"type": "string", "description": "变更原因（可选）"}},
        ["plan_markdown"],
    ),
    _tool(
        "append_note",
        "把本轮教学输出直接归档为单元笔记（禁止重写润色，直接复用会话输出内容）。",
        {"note_title": {"type": "string", "description": "笔记标题，如 单元01-变量与数据类型"},
         "content": {"type": "string", "description": "本轮教学完整输出文本"}},
        ["note_title", "content"],
    ),
    _tool(
        "material_extract",
        "从当前项目原始素材中提取原文（教学引用必须取自返回内容）。三种模式："
        "mode=outline 获取素材大纲（标题结构，开始教学/设计探查题前必用）；"
        "mode=snippet 按关键词提取上下文片段（讲某主题前用）；"
        "mode=full 分块通读全文（offset 定位，max_chars 控制每块长度）。",
        {"material_name": {"type": "string", "description": "素材文件名"},
         "mode": {"type": "string", "description": "snippet（默认）/ outline / full"},
         "query": {"type": "string", "description": "snippet 模式检索关键词，留空返回素材开头"},
         "offset": {"type": "integer", "description": "full 模式读取起点（字符偏移），默认 0"},
         "max_chars": {"type": "integer", "description": "返回最大字符数，默认 3000"}},
        ["material_name"],
    ),
    _tool(
        "add_material",
        "把用户提供的原始素材内容保存到当前项目素材区。",
        {"filename": {"type": "string", "description": "素材文件名，如 主教材.md"},
         "content": {"type": "string", "description": "素材全文"},
         "role": {"type": "string", "description": "main=主素材（默认），supplement=补充素材"}},
        ["filename", "content"],
    ),
    _tool(
        "reload_project",
        "重新从磁盘加载当前项目（素材、计划、元数据），刷新上下文。",
        {},
    ),
    _tool(
        "append_log",
        "追加机器学习元日志（掌握评估、混淆根源、错题、计划变更原因等）。",
        {"log_text": {"type": "string", "description": "日志文本"}},
        ["log_text"],
    ),
    _tool(
        "read_log",
        "读取当前项目机器学习元日志（最近 N 行）。",
        {"tail": {"type": "integer", "description": "读取末尾行数，默认 200"}},
        [],
    ),
    _tool(
        "search_material",
        "在互联网上搜索学习材料（全网搜索 + 维基教科书/维基百科）。当用户需要额外资料/教材/参考书时调用。"
        "支持三个动作：search=只搜索返回结果；import_url=下载指定链接并转为素材；"
        "import_search=把搜索结果中的某条目抓取转为素材。",
        {
            "action": {"type": "string", "description": "search / import_url / import_search"},
            "query": {"type": "string", "description": "搜索主题关键词"},
            "url": {"type": "string", "description": "import_url 时：资料链接（网页/PDF/EPUB/DOCX…）"},
            "title": {"type": "string", "description": "import_search 时：要导入的条目标题"},
            "source": {"type": "string", "description": "import_search 时：条目来源（维基教科书/维基百科/全网搜索）"},
        },
        ["action"],
    ),
    _tool(
        "web_read",
        "直接阅读任意网页/在线资料的内容并返回片段（不保存为素材）。当用户要求你查看某个链接讲了什么、"
        "需要你在线查阅资料辅助讲解时调用；query 可指定要查找的关键词，返回相关上下文片段。",
        {
            "url": {"type": "string", "description": "网页/资料链接"},
            "query": {"type": "string", "description": "可选：要查找的关键词，返回该词附近的上下文片段"},
        },
        ["url"],
    ),
]


# ---------------------------------------------------------------------------
# 工具执行器：function name -> 实现
# ---------------------------------------------------------------------------
def _build_executor(session: Session) -> Callable[[str, dict[str, Any]], Any]:
    def execute(name: str, args: dict[str, Any]) -> Any:
        if name == "list_project":
            return {"projects": pm.list_projects()}
        if name == "create_project":
            info = pm.create_project(args.get("project_name", ""), args.get("description", ""))
            return info
        if name == "open_project":
            from core.session_manager import session_manager
            ctx = session_manager.open_project(session, args["project_id"])
            return ctx
        if name == "save_snapshot":
            if not session.project_id:
                return {"error": "当前未绑定学习项目，无法保存快照"}
            result = pm.save_snapshot(session.project_id, args.get("learner_state", {}),
                                      expected_mtime=session.snapshot_mtime)
            mtime = result.get("latest_mtime")
            if mtime:
                session.snapshot_mtime = mtime
            return result
        if name == "plan_write":
            if not session.project_id:
                return {"error": "当前未绑定学习项目"}
            return pm.plan_write(session.project_id, args.get("plan_markdown", ""),
                                 args.get("change_reason", ""))
        if name == "append_note":
            if not session.project_id:
                return {"error": "当前未绑定学习项目"}
            return pm.append_note(session.project_id, args.get("note_title", ""),
                                  args.get("content", ""))
        if name == "material_extract":
            if not session.project_id:
                return {"error": "当前未绑定学习项目"}
            try:
                return pm.material_extract(
                    session.project_id,
                    args.get("material_name", ""),
                    args.get("query", ""),
                    int(args.get("max_chars") or 3000),
                    args.get("mode", "snippet"),
                    int(args.get("offset") or 0),
                )
            except FileNotFoundError as e:
                return {"error": str(e)}
        if name == "add_material":
            if not session.project_id:
                return {"error": "当前未绑定学习项目"}
            return pm.add_material(session.project_id, args.get("filename", ""),
                                   args.get("content", ""), args.get("role", "main"))
        if name == "search_material":
            from core import material_search as ms
            from core import material_preprocess as pre
            action = args.get("action", "search")
            try:
                if action == "import_url":
                    if not session.project_id:
                        return {"error": "当前未绑定学习项目"}
                    url = args.get("url", "")
                    import tempfile
                    from pathlib import Path
                    tmpdir = Path(tempfile.mkdtemp(prefix="learn_agent_"))
                    try:
                        tmp = tmpdir / "mat.html"
                        ct = ms.download_url(url, str(tmp))
                        ext = ms.guess_extension(ct, url, default=".html")
                        tmp = tmp.rename(tmpdir / ("mat" + ext))
                        md_text, warnings = pre.convert_to_markdown(tmp)
                        parts = pre.split_to_parts(md_text, "网络资料")
                        saved = []
                        for p in parts:
                            r = pm.add_material(session.project_id, p["name"], p["content"], p["role"])
                            saved.append(r.get("material_file") or r.get("filename") or p["name"])
                        return {"ok": True, "imported": saved, "warnings": warnings}
                    finally:
                        import shutil
                        shutil.rmtree(tmpdir, ignore_errors=True)
                if action == "import_search":
                    if not session.project_id:
                        return {"error": "当前未绑定学习项目"}
                    title = args.get("title", "")
                    site = "zh.wikipedia.org" if "维基百科" in (args.get("source") or "") else "zh.wikibooks.org"
                    md_text, real_title = ms.fetch_wiki_page(title, site)
                    parts = pre.split_to_parts(md_text, real_title[:40])
                    saved = []
                    for p in parts:
                        r = pm.add_material(session.project_id, p["name"], p["content"], p["role"])
                        saved.append(r.get("material_file") or r.get("filename") or p["name"])
                    return {"ok": True, "imported": saved}
                # 默认 search
                return {"results": ms.search_materials(args.get("query", ""))}
            except Exception as e:
                return {"error": f"搜索/导入失败：{e}"}
        if name == "web_read":
            from core import material_search as ms
            from core import material_preprocess as pre
            url = args.get("url", "")
            query = (args.get("query") or "").strip()
            if not url.startswith(("http://", "https://")):
                return {"error": "请输入有效的 http(s) 链接"}
            import tempfile
            from pathlib import Path
            tmpdir = Path(tempfile.mkdtemp(prefix="learn_read_"))
            try:
                tmp = tmpdir / "page.html"
                ct = ms.download_url(url, str(tmp))
                ext = ms.guess_extension(ct, url, default=".html")
                tmp = tmp.rename(tmpdir / ("page" + ext))
                md_text, _ = pre.convert_to_markdown(tmp)
                text = md_text or ""
                if not text.strip():
                    return {"error": "该页面无可读文本内容（可能是反爬或纯脚本页面）"}
                lines = text.splitlines()
                if query:
                    hits = [i for i, ln in enumerate(lines) if query in ln]
                    if hits:
                        i = hits[0]
                        snippet = "\n".join(lines[max(0, i - 15): i + 30])
                    else:
                        snippet = "\n".join(lines[:80])
                    snippet = snippet[:6000]
                else:
                    snippet = "\n".join(lines[:120])[:6000]
                return {
                    "url": url, "query": query,
                    "snippet": snippet,
                    "total_chars": len(text),
                    "note": "如需全文，可用 query 指定关键词继续分段阅读",
                }
            except Exception as e:
                return {"error": f"网页读取失败：{e}（该站点可能有反爬限制）"}
            finally:
                import shutil
                shutil.rmtree(tmpdir, ignore_errors=True)
        if name == "reload_project":
            if not session.project_id:
                return {"error": "当前未绑定学习项目"}
            ctx = pm.reload_project(session.project_id)
            session.learner_state = ctx["learner_state"]
            return ctx
        if name == "append_log":
            if not session.project_id:
                return {"error": "当前未绑定学习项目"}
            return pm.append_log(session.project_id, args.get("log_text", ""))
        if name == "read_log":
            if not session.project_id:
                return {"error": "当前未绑定学习项目"}
            return {"log": pm.read_log(session.project_id, int(args.get("tail", 200)))}
        return {"error": f"未知工具: {name}"}
    return execute


# ---------------------------------------------------------------------------
# System Prompt 组装
# ---------------------------------------------------------------------------
def _refresh_project_context(session: Session) -> None:
    """每次对话前同步会话与磁盘的项目状态：
    - 素材上传/删除后，session.mode 可能已过期（如 general → document_anchor）
    - learner_state 以磁盘快照为权威，若有更新同步回内存
    """
    if not session or not session.project_id:
        return
    try:
        ctx = pm.load_project(session.project_id)
    except Exception:
        return
    session.project_name = ctx["meta"]["project_name"]
    session.mode = ctx["meta"]["mode"]
    if ctx.get("learner_state"):
        session.learner_state = ctx["learner_state"]


def _material_context(session: Session) -> list[str]:
    """实时读取项目素材列表（磁盘为权威），供 system prompt 注入。"""
    if not session or not session.project_id:
        return []
    try:
        ctx = pm.load_project(session.project_id)
    except Exception:
        return []
    active = set(ctx.get("meta", {}).get("active_material_set") or [])
    supp = set(ctx.get("meta", {}).get("supplementary_materials") or [])
    lines = []
    for m in ctx.get("materials") or []:
        role = "主素材" if m in active else ("补充素材" if m in supp else "")
        lines.append(f"  - {m} {role}".rstrip())
    return lines


def build_system_prompt(session: Session | None = None) -> str:
    try:
        skill_text = SKILL_PATH.read_text(encoding="utf-8")
    except OSError:
        skill_text = "（skill.md 缺失）"

    parts = [skill_text, "\n\n===== 运行时上下文（后端注入，每次对话刷新） ====="]
    if session is None:
        parts.append("- 当前状态：全局主菜单（未绑定学习项目）")
    else:
        parts.append(f"- 当前状态：项目内学习")
        parts.append(f"- 绑定项目：{session.project_name}（id={session.project_id}）")
        parts.append(f"- 运行模式：{session.mode}（document_anchor=文档锚定，general=通用学习）")
        mats = _material_context(session)
        if mats:
            parts.append(f"- 项目素材（{len(mats)} 份，实时磁盘状态，教学优先引用）：\n" + "\n".join(mats))
        else:
            parts.append("- 项目素材：无（当前为通用知识模式，基于内置知识教学）")
        if session.learner_state:
            parts.append("- 学习者状态：\n```json\n"
                         + json.dumps(session.learner_state, ensure_ascii=False, indent=2)
                         + "\n```")
    parts.append(
        "\n【工具调用规则】\n"
        "1. 需要读写文件、持久化状态时，必须调用对应工具，禁止虚构结果；\n"
        "2. 工具调用后根据返回结果继续生成回复；\n"
        "3. 教学引用原文必须来自 material_extract 返回的片段；\n"
        "4. 每轮教学闭环必须调用 append_note 归档笔记、save_snapshot 保存状态、"
        "append_log 追加学习日志（记录本轮教学单元、掌握评估、混淆根源、错题），"
        "除非用户明确不需要。\n"
        "\n【文档锚定模式教学流程（强制，不得跳过）】\n"
        "当运行模式为 document_anchor 时：\n"
        "1. 开始教学（含探查题设计、计划生成）之前，必须先调用 material_extract(mode=outline) "
        "获取素材大纲，再按需用 mode=snippet / mode=full 精读相关章节；\n"
        "2. 探查题、教学讲解必须锚定素材实际内容——题目应取材自素材的章节/观点/原文表述，"
        "不得脱离素材凭空出通用题；\n"
        "3. 讲解引用原文时，在回复中标注出处（如：出自《子平真诠》某章）；\n"
        "4. 若素材为空或 outline 无结果，才允许退回通用知识模式。"
    )
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# LLM 调用
# ---------------------------------------------------------------------------
def _chat_completion(messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None) -> dict[str, Any]:
    cfg = get_llm_config()
    if not cfg["api_key"]:
        raise RuntimeError("未配置 LLM API Key（可在系统设置中填写，或设置环境变量 LEARN_LLM_API_KEY）。")

    url = cfg["base_url"].rstrip("/") + "/chat/completions"
    payload: dict[str, Any] = {
        "model": cfg["model"],
        "messages": messages,
        "temperature": 0.6,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"

    proxy = get_http_proxy()
    client_kwargs: dict[str, Any] = {"timeout": float(cfg["timeout"])}
    if proxy:
        client_kwargs["proxy"] = proxy
    with httpx.Client(**client_kwargs) as client:
        resp = client.post(url, json=payload, headers={
            "Authorization": f"Bearer {cfg['api_key']}",
            "Content-Type": "application/json",
        })
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError:
            detail = ""
            try:
                detail = resp.text[:400]
            except Exception:
                pass
            raise RuntimeError(f"LLM 接口返回 {resp.status_code}：{detail}")
        return resp.json()


def run_conversation(session: Session, user_message: str) -> dict[str, Any]:
    """执行一轮对话：组装消息 → LLM 循环（含工具调用）→ 返回最终回复。"""
    # 每次对话前刷新项目上下文：素材/模式/学习者状态以磁盘为权威，
    # 避免旧会话感知不到上传后的新素材（general → document_anchor）
    _refresh_project_context(session)
    system_prompt = build_system_prompt(session)

    messages: list[dict[str, Any]] = [{"role": "system", "content": system_prompt}]
    # 会话历史（保留最近 40 条）
    messages.extend(session.messages[-40:])
    messages.append({"role": "user", "content": user_message})

    executor = _build_executor(session)
    final_text = ""
    tool_events: list[dict[str, Any]] = []

    for _round in range(12):  # 工具循环上限，防死循环
        try:
            data = _chat_completion(messages, TOOLS)
        except Exception as e:
            return {"error": f"LLM 调用失败：{e}", "tool_events": tool_events}

        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message", {})
        content = message.get("content") or ""
        tool_calls = message.get("tool_calls") or []

        if not tool_calls:
            final_text = content
            break

        # 有工具调用：追加 assistant 消息 + 依次执行
        messages.append({"role": "assistant", "content": content or "", "tool_calls": tool_calls})
        for tc in tool_calls:
            fn_name = tc["function"]["name"]
            try:
                fn_args = json.loads(tc["function"].get("arguments") or "{}")
            except json.JSONDecodeError:
                fn_args = {}
            try:
                result = executor(fn_name, fn_args)
            except Exception as e:
                result = {"error": f"工具执行异常：{e}"}
            tool_events.append({"tool": fn_name, "args": fn_args, "result": result})
            messages.append({
                "role": "tool",
                "tool_call_id": tc["id"],
                "content": json.dumps(result, ensure_ascii=False),
            })

    if not final_text:
        final_text = "（模型未返回有效回复，请重试或检查 LLM 配置。）"

    # 更新会话历史：只保留 user 消息 + 有实际内容的 assistant 回复，
    # 跳过工具调用中间轮（assistant content 常为空），并剥离 tool_calls 字段
    # （DeepSeek 要求带 tool_calls 的 assistant 消息必须紧跟 tool 响应，
    #   历史只留纯文本，避免下次请求 400；空消息也会渲染成空气泡）
    history: list[dict[str, Any]] = []
    for m in messages[1:]:
        if m["role"] == "user":
            history.append(m)
        elif m["role"] == "assistant" and (m.get("content") or "").strip():
            history.append({k: v for k, v in m.items() if k != "tool_calls"})
    session.messages = history[-40:]
    # 每轮对话后立即落盘：刷新页面/服务重启不丢对话（此前只在切项目/退出/闲置回收时保存）
    try:
        if session.project_id and session.current_thread:
            pt.save_messages(session.project_id, session.current_thread, session.messages)
    except Exception:
        pass
    session.touch()
    return {"reply": final_text, "tool_events": tool_events}

"""项目相关 API：列表 / 新建 / 详情 / 笔记 / 计划 / 素材 / 删除。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from pydantic import BaseModel

from auth import get_current_user
from core import project_manager as pm
from core import material_preprocess as pre
from core import material_search as ms

router = APIRouter(prefix="/api/projects", tags=["projects"])

def _check_owner(project_id: str, user: dict) -> None:
    """项目归属校验：admin 可访问全部，普通用户仅能访问自己的项目。"""
    owner = pm._owner_of(project_id)
    if user.get("role") != "admin" and owner != user.get("id"):
        raise HTTPException(status_code=403, detail="无权访问该项目")



class CreateProjectBody(BaseModel):
    project_name: str
    description: str = ""


class AddMaterialBody(BaseModel):
    filename: str
    content: str
    role: str = "main"


class FileUpdateBody(BaseModel):
    content: str


class FileCreateBody(BaseModel):
    name: str
    content: str = ""


class FileRenameBody(BaseModel):
    new_name: str


class SearchBody(BaseModel):
    query: str


class ImportSearchBody(BaseModel):
    title: str
    source: str = "维基教科书"
    url: str = ""


class ImportUrlBody(BaseModel):
    url: str
    name: str = ""


@router.get("")
def list_projects(user: dict = Depends(get_current_user)):
    owner = "" if user.get("role") == "admin" else user.get("id")
    return {"projects": pm.list_projects(owner=owner)}


@router.post("")
def create_project(body: CreateProjectBody, user: dict = Depends(get_current_user)):
    try:
        return pm.create_project(body.project_name, body.description, owner=user.get("id"))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/{project_id}")
def get_project(project_id: str, user: dict = Depends(get_current_user)):
    _check_owner(project_id, user)
    try:
        return pm.load_project(project_id)
    except (ValueError, FileNotFoundError) as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/{project_id}/notes")
def get_notes(project_id: str, user: dict = Depends(get_current_user)):
    _check_owner(project_id, user)
    try:
        pdir = pm._project_dir(project_id)
        notes_dir = pm._notes_dir(project_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    notes = []
    for f in sorted(notes_dir.glob("*.md"), key=lambda p: p.name):
        if f.name == "README.md":
            continue
        notes.append({"name": f.name,
                      "content": f.read_text(encoding="utf-8", errors="replace")})
    return {"notes": notes, "notes_root": str(notes_dir)}


@router.get("/{project_id}/plan")
def get_plan(project_id: str, user: dict = Depends(get_current_user)):
    _check_owner(project_id, user)
    try:
        return {
            "versions": pm.list_plan_versions(project_id),
            "latest": pm.load_project(project_id)["plan_latest"],
        }
    except (ValueError, FileNotFoundError) as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/{project_id}/plan/{file_name}")
def get_plan_version(project_id: str, file_name: str, user: dict = Depends(get_current_user)):
    """读取指定计划文件内容（plan_latest.md 或历史版本 plan_vN.md）。"""
    _check_owner(project_id, user)
    try:
        content = pm.read_plan_file(project_id, file_name)
        return {"name": file_name, "content": content}
    except (ValueError, FileNotFoundError) as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/{project_id}/materials")
def get_materials(project_id: str, user: dict = Depends(get_current_user)):
    _check_owner(project_id, user)
    try:
        ctx = pm.load_project(project_id)
        return {"materials": ctx["materials"]}
    except (ValueError, FileNotFoundError) as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/{project_id}/materials/{material_name}")
def get_material(project_id: str, material_name: str, user: dict = Depends(get_current_user)):
    _check_owner(project_id, user)
    try:
        pdir = pm._project_dir(project_id)
        src = pdir / pm.CONFIG_DIR_NAME / pm.SOURCE_DIR
        target = src / material_name
        if not target.is_file():
            raise FileNotFoundError(material_name)
        return {"name": target.name,
                "content": target.read_text(encoding="utf-8", errors="replace")}
    except (ValueError, FileNotFoundError) as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/{project_id}/upload", summary="上传原始材料并预处理为 Markdown")
async def upload_material(project_id: str, file: UploadFile = File(...),
                          user: dict = Depends(get_current_user)):
    """上传任意可处理文件（EPUB/PDF/DOCX/TXT/MD/HTML/PPTX/XLSX…），
    自动转换为 Markdown；扫描版 PDF 走 OCR；超长文档自动拆分。

    目录分流：
    - 预处理成功 → Markdown 进 source_material/（AI 可用），原始文件副本进 _源文件/
    - 预处理失败 → 原始文件进 _无法处理/（保留待重试或删除），不阻塞导入
    """
    _check_owner(project_id, user)
    import tempfile
    from pathlib import Path

    filename = file.filename or "素材"
    ext = Path(filename).suffix.lower()
    tmpdir = Path(tempfile.mkdtemp(prefix="learn_upload_"))
    tmp = tmpdir / filename
    try:
        content = await file.read()
        tmp.write_bytes(content)

        if not pre.is_supported(filename):
            # 不支持的格式：保留到 _无法处理/，不阻塞
            quar_name = pm.save_raw_file(project_id, filename, content, kind="quarantine")
            return {
                "converted": False,
                "source": filename,
                "reason": f"暂不支持 {ext or '该'} 格式，已放入「无法处理」目录。可处理：EPUB / PDF / DOCX / TXT / MD / HTML / PPTX / XLSX / CSV",
                "quarantined": quar_name,
            }

        try:
            md_text, warnings = pre.convert_to_markdown(tmp)
        except Exception as e:
            quar_name = pm.save_raw_file(project_id, filename, content, kind="quarantine")
            return {
                "converted": False,
                "source": filename,
                "reason": f"预处理失败：{e}。原始文件已放入「无法处理」目录，可稍后重试或删除。",
                "quarantined": quar_name,
            }

        base = Path(filename).stem[:60]
        parts = pre.split_to_parts(md_text, base)
        if not parts:
            quar_name = pm.save_raw_file(project_id, filename, content, kind="quarantine")
            return {
                "converted": False,
                "source": filename,
                "reason": "未能从文件中提取到任何文本（可能是纯图片且 OCR 失败）。原始文件已放入「无法处理」目录。",
                "quarantined": quar_name,
            }

        saved = []
        for p in parts:
            r = pm.add_material(project_id, p["name"], p["content"], p["role"])
            saved.append({"name": r.get("material_file") or r.get("filename") or p["name"], "role": p["role"]})

        # 保留原始格式文件副本到 _源文件/（AI 不可读，仅保留待用）
        try:
            pm.save_raw_file(project_id, filename, content, kind="source")
        except Exception:
            pass

        return {
            "converted": True,
            "source": filename,
            "format": ext.lstrip("."),
            "parts": saved,
            "part_count": len(saved),
            "warnings": warnings,
        }
    finally:
        try:
            tmp.unlink(missing_ok=True)
            tmpdir.rmdir()
        except Exception:
            pass


@router.get("/{project_id}/raw-files")
def list_raw_files(project_id: str, kind: str = "source", user: dict = Depends(get_current_user)):
    """列出素材子目录文件。kind = source（源文件） | quarantine（无法处理）。"""
    _check_owner(project_id, user)
    if kind not in ("source", "quarantine"):
        raise HTTPException(status_code=400, detail="kind 仅支持 source / quarantine")
    try:
        return {"kind": kind, "files": pm.list_raw_files(project_id, kind)}
    except (ValueError, FileNotFoundError) as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.delete("/{project_id}/raw-files/{kind}/{name}")
def delete_raw_file(project_id: str, kind: str, name: str, user: dict = Depends(get_current_user)):
    """删除素材子目录文件（进回收站，可恢复）。"""
    _check_owner(project_id, user)
    if kind not in ("source", "quarantine"):
        raise HTTPException(status_code=400, detail="kind 仅支持 source / quarantine")
    try:
        return pm.delete_raw_file(project_id, kind, name)
    except (ValueError, FileNotFoundError) as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/{project_id}/raw-files/quarantine/{name}/reprocess")
def reprocess_quarantine(project_id: str, name: str, user: dict = Depends(get_current_user)):
    """重新预处理「无法处理」目录中的文件：成功则转为素材并从子目录移除。"""
    _check_owner(project_id, user)
    try:
        path = pm.read_raw_file(project_id, "quarantine", name)
        md_text, warnings = pre.convert_to_markdown(path)
        base = Path(name).stem[:60]
        parts = pre.split_to_parts(md_text, base)
        if not parts:
            raise ValueError("未能提取到任何文本")
        saved = []
        for p in parts:
            r = pm.add_material(project_id, p["name"], p["content"], p["role"])
            saved.append({"name": r.get("material_file") or r.get("filename") or p["name"], "role": p["role"]})
        path.unlink(missing_ok=True)
        return {"converted": True, "parts": saved, "part_count": len(saved), "warnings": warnings}
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"重新预处理失败：{e}")


@router.post("/{project_id}/materials")
def add_material(project_id: str, body: AddMaterialBody, user: dict = Depends(get_current_user)):
    _check_owner(project_id, user)
    try:
        return pm.add_material(project_id, body.filename, body.content, body.role)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ---------------------------------------------------------------------------
# 引导期：资料准备建议（AI 分析主题 → 搜索 → 筛选推荐）
# ---------------------------------------------------------------------------
def _parse_llm_json(content: str) -> list:
    """从 LLM 输出中稳健提取 JSON 列表（容忍 ```json 包裹与前后杂讯）。"""
    import re, json as _json
    text = (content or "").strip()
    # 去掉 ```json ... ``` 代码块
    m = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if m:
        text = m.group(1).strip()
    # 找第一个 [ 到最后一个 ]
    s, e = text.find("["), text.rfind("]")
    if s != -1 and e > s:
        text = text[s:e + 1]
    try:
        data = _json.loads(text)
        return data if isinstance(data, list) else []
    except Exception:
        return []


@router.post("/{project_id}/guide-material")
def guide_material(project_id: str, user: dict = Depends(get_current_user)):
    """进入项目引导时的准备分析：AI 先分析当前状态与资料缺口，再搜索推荐。

    返回 {"need_material", "gap", "reason", "recommendations", "analysis_text"}
    analysis_text 为面向用户的准备分析（状态摘要 + 资料结论 + 下一步引导），
    前端作为 AI 进入项目后的第一条消息显示。
    """
    _check_owner(project_id, user)
    from core.agent_bridge import _chat_completion
    ctx = pm.load_project(project_id)
    meta = ctx["meta"]
    topic = meta.get("project_name", "") or "学习"
    mode = meta.get("mode", "general")
    materials = ctx.get("materials") or []
    plan = (ctx.get("plan_latest") or "").strip()
    state = ctx.get("learner_state") or {}

    # 反爬站无法导入，不进入推荐候选
    BLOCKED_HOSTS = ("baike.baidu.com", "zhihu.com", "weixin.qq.com", "juejin.cn")

    # 1) AI 判断：是否需要资料、缺什么，并生成面向用户的准备分析
    mat_list = "\n".join(f"- {m}" for m in materials[:15]) or "（无）"
    plan_snippet = plan[:400] if plan else "（尚无学习计划）"
    state_snippet = ""
    if state.get("current_teaching_unit"):
        state_snippet = f"当前学到：{state['current_teaching_unit']}；"
    if state.get("mastered"):
        state_snippet += f"已掌握：{', '.join(state['mastered'][:3])}"
    judge_prompt = (
        f"你是学习资料顾问。学习项目【{topic}】，模式：{'文档锚定' if mode == 'document_anchor' else '通用知识'}。\n"
        f"已有素材：\n{mat_list}\n学习计划节选：{plan_snippet}\n学习者状态：{state_snippet or '空白'}\n\n"
        "请判断当前**是否需要**从网络补充学习资料，以及**缺什么**。判断原则：\n"
        "1. 素材已覆盖主题核心（如教材/讲义齐全）且够用 → 不需要，避免资料冗余；\n"
        "2. 无素材（通用模式）或素材明显缺失关键方面（如只有概述缺深入讲解/缺练习/缺图解）→ 需要，并说明缺什么；\n"
        "3. 不要无脑建议拓展；只在有明确缺口时建议。\n"
        '输出 JSON：{"need": true/false, "gap": "缺什么（一句话，不需要则空字符串）", "reason": "判断理由（一句话）", '
        '"analysis": "面向用户的准备分析（2-3句，中文，直接可展示）：①当前状态摘要（素材/计划/进度）；②资料准备结论'
        '（需要资料则说明缺什么并提示：回复「要」我可搜集资料；不需要则说明现有够用、无需额外准备）；③下一步提示'
        '（提示用户回复「开始」或任意内容即可进入学习）。"}。'
        "只输出 JSON。"
    )
    need, gap, judge_reason, analysis = False, "", "", ""
    try:
        data = _chat_completion([
            {"role": "system", "content": "你是严谨的学习资料顾问，只输出合法 JSON。"},
            {"role": "user", "content": judge_prompt},
        ], None)
        content = (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
        import re, json as _json
        m = re.search(r"\{[\s\S]*\}", content)
        if m:
            obj = _json.loads(m.group(0))
            need = bool(obj.get("need"))
            gap = str(obj.get("gap") or "")
            judge_reason = str(obj.get("reason") or "")
            analysis = str(obj.get("analysis") or "").strip()
    except Exception:
        # 判断失败兜底：无素材才建议收集
        need = len(materials) == 0
        gap = "系统未识别出明确缺口" if need else ""
        judge_reason = "自动判断" if need else ""

    if not analysis:
        if need:
            analysis = (f"项目【{topic}】目前{'没有任何素材，' if not materials else ''}"
                        f"{'缺少' + gap + '，' if gap else ''}建议先准备资料。回复「要」我可帮你搜集网络资料。"
                        f"或回复「开始」直接开始学习。")
        else:
            analysis = (f"项目【{topic}】状态良好：{'已有 ' + str(len(materials)) + ' 份素材' if materials else '暂无素材'}，"
                        f"现有准备已足够支撑学习，无需额外拓展资料。回复「开始」即可进入学习。")

    if not need:
        return {
            "need_material": False, "gap": gap,
            "reason": judge_reason or "现有素材已足够支撑当前学习，无需额外拓展",
            "recommendations": [],
            "analysis_text": analysis,
        }

    # 2) 需要资料 → 按缺口搜索（国内源优先，过滤反爬站）
    seen: set[str] = set()
    results: list[dict] = []
    for kw in (gap or topic, topic, f"{topic} 教程"):
        try:
            for r in ms.search_materials(kw)[:8]:
                host = (r.get("url") or "").split("/")[2] if "//" in (r.get("url") or "") else ""
                if host and any(b in host for b in BLOCKED_HOSTS):
                    continue
                if r["url"] not in seen:
                    seen.add(r["url"])
                    results.append(r)
        except Exception:
            continue
        if len(results) >= 12:
            break
    if not results:
        return {"need_material": True, "gap": gap, "reason": judge_reason,
                "recommendations": [], "analysis_text": analysis}

    # 3) LLM 按缺口筛选 3-5 条
    cand_text = "\n".join(
        f"- [{i}] 标题：{r['title']}\n  来源：{r['source']}\n  链接：{r['url']}\n  摘要：{r.get('snippet', '')[:100]}"
        for i, r in enumerate(results)
    )
    pick_prompt = (
        f"项目【{topic}】当前缺口：{gap}（{judge_reason}）。候选资料：\n{cand_text}\n\n"
        "请从中挑选 3-5 条最能补上该缺口的资料，输出 JSON 数组，每项："
        '{"index": 候选编号, "reason": "为什么能补缺口"}。只输出 JSON，都不合适则输出 []。'
    )
    recs = []
    try:
        data = _chat_completion([
            {"role": "system", "content": "你是严谨的资料推荐助手，只输出合法 JSON。"},
            {"role": "user", "content": pick_prompt},
        ], None)
        content = (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
        for p in _parse_llm_json(content):
            try:
                r = results[int(p.get("index"))]
            except Exception:
                continue
            recs.append({"title": r["title"], "url": r["url"],
                         "source": r["source"], "reason": p.get("reason", "")})
    except Exception:
        pass
    if not recs:
        recs = [{"title": r["title"], "url": r["url"], "source": r["source"], "reason": "补缺口候选"}
                for r in results[:4]]

    return {"need_material": True, "gap": gap,
            "reason": judge_reason or "建议按缺口补充资料",
            "recommendations": recs, "analysis_text": analysis}
@router.post("/{project_id}/search-materials")
def search_materials(project_id: str, body: SearchBody, user: dict = Depends(get_current_user)):
    """按主题搜索可用学习材料（维基教科书 + 维基百科）。"""
    _check_owner(project_id, user)
    query = (body.query or "").strip()
    if not query:
        raise HTTPException(status_code=400, detail="请输入搜索主题")
    try:
        results = ms.search_materials(query)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"搜索失败（检查网络/代理）：{e}")
    return {"query": query, "results": results}


@router.post("/{project_id}/import-search")
def import_search(project_id: str, body: ImportSearchBody, user: dict = Depends(get_current_user)):
    """导入维基搜索结果的条目：抓取正文 → 转 Markdown → 存为素材。"""
    _check_owner(project_id, user)
    title = (body.title or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="缺少条目标题")
    site = "zh.wikipedia.org" if "维基百科" in (body.source or "") else "zh.wikibooks.org"
    try:
        md_text, real_title = ms.fetch_wiki_page(title, site)
        if not md_text.strip():
            raise ValueError("条目正文为空")
        base = real_title[:40]
        parts = pre.split_to_parts(md_text, base)
        saved = []
        for p in parts:
            r = pm.add_material(project_id, p["name"], p["content"], p["role"])
            saved.append({"name": r.get("material_file") or r.get("filename") or p["name"], "role": p["role"]})
        return {"converted": True, "source": real_title, "parts": saved, "part_count": len(saved)}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"导入失败：{e}")


@router.post("/{project_id}/import-url")
def import_url(project_id: str, body: ImportUrlBody, user: dict = Depends(get_current_user)):
    """下载任意资料链接（网页/PDF/EPUB/DOCX…）→ 预处理 → 存为素材。"""
    _check_owner(project_id, user)
    import tempfile
    from pathlib import Path

    url = (body.url or "").strip()
    if not url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="请输入以 http(s):// 开头的链接")
    import re
    raw_name = (body.name or "").strip() or "网络资料"
    # Windows 文件名非法字符消毒
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", raw_name).strip(" .") or "网络资料"
    name = name[:50]

    tmpdir = Path(tempfile.mkdtemp(prefix="learn_net_"))
    try:
        ext = ".html"
        tmp = tmpdir / (f"{name}{ext}")
        content_type = ms.download_url(url, str(tmp))
        ext = ms.guess_extension(content_type, url, default=".html")
        tmp = tmp.rename(tmpdir / (name + ext))

        md_text, warnings = pre.convert_to_markdown(tmp)
        parts = pre.split_to_parts(md_text, name[:40])
        if not parts:
            raise HTTPException(status_code=400, detail="未能从链接内容中提取到文本")
        saved = []
        for p in parts:
            r = pm.add_material(project_id, p["name"], p["content"], p["role"])
            saved.append({"name": r.get("material_file") or r.get("filename") or p["name"], "role": p["role"]})
        return {"converted": True, "source": url, "parts": saved, "part_count": len(saved), "warnings": warnings}
    except HTTPException:
        raise
    except Exception as e:
        msg = str(e)
        if "403" in msg:
            msg += "（该站点有反爬限制，建议换一个结果或粘贴可访问的链接）"
        raise HTTPException(status_code=502, detail=f"下载/导入失败：{msg}")
    finally:
        try:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)
        except Exception:
            pass


@router.get("/{project_id}/snapshot")
def get_snapshot(project_id: str, user: dict = Depends(get_current_user)):
    _check_owner(project_id, user)
    try:
        ctx = pm.load_project(project_id)
        return {"learner_state": ctx["learner_state"], "meta": ctx["snapshot_meta"]}
    except (ValueError, FileNotFoundError) as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/{project_id}/log")
def get_log(project_id: str, user: dict = Depends(get_current_user)):
    _check_owner(project_id, user)
    try:
        return {"log": pm.read_log(project_id)}
    except (ValueError, FileNotFoundError) as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.delete("/{project_id}")
def delete_project(project_id: str, user: dict = Depends(get_current_user)):
    _check_owner(project_id, user)
    try:
        return pm.delete_project(project_id)
    except (ValueError, FileNotFoundError) as e:
        raise HTTPException(status_code=404, detail=str(e))


# ---------------------------------------------------------------------------
# md 文件增删改（material / note / plan）
# ---------------------------------------------------------------------------
@router.put("/{project_id}/files/{kind}/{name}")
def update_file(project_id: str, kind: str, name: str, body: FileUpdateBody, user: dict = Depends(get_current_user)):
    _check_owner(project_id, user)
    try:
        return pm.update_file(project_id, kind, name, body.content)
    except (ValueError, FileNotFoundError) as e:
        raise HTTPException(status_code=404 if isinstance(e, FileNotFoundError) else 400,
                            detail=str(e))


@router.post("/{project_id}/files/{kind}")
def create_file(project_id: str, kind: str, body: FileCreateBody, user: dict = Depends(get_current_user)):
    _check_owner(project_id, user)
    try:
        if kind == "note":
            return pm.create_note(project_id, body.name, body.content)
        if kind == "material":
            return pm.add_material(project_id, body.name, body.content, "main")
        raise ValueError("仅支持创建 note / material 文件")
    except (ValueError, FileNotFoundError) as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/{project_id}/files/{kind}/{name}")
def delete_file(project_id: str, kind: str, name: str, user: dict = Depends(get_current_user)):
    _check_owner(project_id, user)
    try:
        return pm.delete_file(project_id, kind, name)
    except (ValueError, FileNotFoundError) as e:
        raise HTTPException(status_code=404 if isinstance(e, FileNotFoundError) else 400,
                            detail=str(e))


# ---------------------------------------------------------------------------
# 回收站（防误删）
# ---------------------------------------------------------------------------
@router.get("/{project_id}/trash")
def list_trash(project_id: str, user: dict = Depends(get_current_user)):
    _check_owner(project_id, user)
    try:
        return {"items": pm.list_trash(project_id)}
    except FileNotFoundError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/{project_id}/trash/{trash_name}/restore")
def restore_file(project_id: str, trash_name: str, user: dict = Depends(get_current_user)):
    _check_owner(project_id, user)
    try:
        return pm.restore_file(project_id, trash_name)
    except (FileNotFoundError, ValueError) as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/{project_id}/trash/{trash_name}")
def purge_file(project_id: str, trash_name: str, user: dict = Depends(get_current_user)):
    _check_owner(project_id, user)
    try:
        return pm.purge_file(project_id, trash_name)
    except FileNotFoundError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/{project_id}/files/{kind}/{name}/rename")
def rename_file(project_id: str, kind: str, name: str, body: FileRenameBody, user: dict = Depends(get_current_user)):
    _check_owner(project_id, user)
    try:
        return pm.rename_file(project_id, kind, name, body.new_name)
    except (ValueError, FileNotFoundError) as e:
        raise HTTPException(status_code=404 if isinstance(e, FileNotFoundError) else 400,
                            detail=str(e))

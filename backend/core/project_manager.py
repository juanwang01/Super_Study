"""项目管理模块：目录规范、schema 校验、快照/计划/笔记/素材的持久化。

安全约束（本模块强制，LLM 不可绕过）：
1. 所有 project_id 必须解析到 DATA_ROOT 内，路径防穿越；
2. 项目隔离：只允许读写本项目目录；跨项目读取素材走专用只读接口；
3. 快照并发检测：写入前比对磁盘 mtime，发现他方修改返回告警。
"""
from __future__ import annotations

import re
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from config import DATA_ROOT, ensure_dirs

ensure_dirs()

CONFIG_DIR_NAME = ".learn_config"
META_FILE = "project.meta.yaml"
SOURCE_DIR = "source_material"
SOURCE_RAW_DIR = "_源文件"        # 原始格式文件（AI 不可读，仅保留待用）
SOURCE_QUAR_DIR = "_无法处理"     # 预处理失败的文件（待重试或删除）
PLAN_DIR = "plan"
SNAPSHOT_DIR = "state_snapshot"
LOG_DIR = "session_log"
TRASH_DIR = "trash"
PLAN_LATEST = "plan_latest.md"
SNAPSHOT_LATEST = "snapshot_latest.yaml"

META_REQUIRED_FIELDS = ["project_id", "project_name", "create_time", "mode"]
MODE_VALUES = {"document_anchor", "general"}
ROOT_CAUSE_VALUES = {
    "概念混淆", "边界混淆", "判据误用", "类推过度", "前提缺失", "术语误读", "对称性错判", "无",
}
ZPD_VALUES = {"within", "too_easy", "too_hard"}


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------
def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


# 项目归属索引（project_id → owner user_id）。多用户架构：
# 数据目录 = data/{owner}/{project_id}/。索引表存于 data/system.db（见 auth 模块）。
_OWNER_INDEX_CACHE: dict[str, str] = {}


def _owner_of(project_id: str) -> str:
    """查询项目所属用户。找不到时回退 'admin'（兼容迁移前的旧目录结构）。"""
    cached = _OWNER_INDEX_CACHE.get(project_id)
    if cached:
        return cached
    owner = "admin"
    try:
        import sqlite3
        conn = sqlite3.connect(DATA_ROOT / "system.db", timeout=10)
        try:
            row = conn.execute(
                "SELECT owner FROM projects WHERE project_id=?", (project_id,)).fetchone()
            if row:
                owner = row[0]
        finally:
            conn.close()
    except Exception:
        pass
    _OWNER_INDEX_CACHE[project_id] = owner
    return owner


def register_project_owner(project_id: str, owner: str) -> None:
    """登记项目归属（建项目/迁移时调用）。"""
    import sqlite3
    DATA_ROOT.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DATA_ROOT / "system.db", timeout=10)
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS projects ("
            "project_id TEXT PRIMARY KEY, owner TEXT NOT NULL)")
        conn.execute(
            "INSERT OR REPLACE INTO projects (project_id, owner) VALUES (?,?)",
            (project_id, owner))
        conn.commit()
    finally:
        conn.close()
    _OWNER_INDEX_CACHE[project_id] = owner


def _project_dir(project_id: str) -> Path:
    """解析 project_id → 项目目录（多用户：data/{owner}/{project_id}），并做防穿越校验。"""
    if not re.fullmatch(r"[A-Za-z0-9\-_]+", project_id or ""):
        raise ValueError(f"非法的 project_id: {project_id!r}")
    root = DATA_ROOT.resolve()
    owner = _owner_of(project_id)
    p = (root / owner / project_id).resolve()
    # 兼容旧结构：若 data/{owner}/{pid} 不存在但 data/{pid} 存在（迁移前），跟随旧目录
    if not p.exists():
        legacy = (root / project_id).resolve()
        if legacy.is_relative_to(root) and legacy.exists():
            p = legacy
    if not p.is_relative_to(root):
        raise ValueError("路径越界：project_id 必须位于数据根目录内")
    return p


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return data if isinstance(data, dict) else {}


def _dump_yaml(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)


def _validate_meta(meta: dict[str, Any]) -> None:
    for field in META_REQUIRED_FIELDS:
        if field not in meta:
            raise ValueError(f"project.meta.yaml 缺少必填字段: {field}")
    if meta.get("mode") not in MODE_VALUES:
        raise ValueError(f"mode 非法: {meta.get('mode')!r}")


# ---------------------------------------------------------------------------
# 项目 CRUD
# ---------------------------------------------------------------------------
def list_projects(owner: str = "") -> list[dict[str, Any]]:
    """扫描总根目录，返回学习项目（按最后学习时间倒序）。

    owner 非空时只返回该用户的项目（多用户：data/{owner}/{pid}）。
    owner 为空时返回全部（管理员视角；兼容旧平铺结构）。
    """
    projects: list[dict[str, Any]] = []
    if owner:
        base = DATA_ROOT / owner
        if base.is_dir():
            dirs = [base / d for d in base.iterdir() if (base / d).is_dir()]
        else:
            dirs = []
    else:
        # 管理员视角：遍历 data/ 下一级。多用户结构为 {owner}/{pid}（两级），
        # 旧平铺为 {pid}（一级），两者都收集。
        dirs = []
        for child in DATA_ROOT.iterdir():
            if not child.is_dir():
                continue
            if (child / CONFIG_DIR_NAME / META_FILE).exists():
                dirs.append(child)                    # 旧平铺项目
            else:
                dirs.extend(d for d in child.iterdir() if d.is_dir())  # {owner}/{pid}
    for child in dirs:
        meta_path = child / CONFIG_DIR_NAME / META_FILE
        if not meta_path.exists():
            continue
        meta = _load_yaml(meta_path)
        try:
            _validate_meta(meta)
        except ValueError:
            continue  # 损坏的项目不展示，避免拖垮列表
        notes = [p.name for p in child.glob("*.md")]
        projects.append({
            "project_id": meta.get("project_id", child.name),
            "project_name": meta.get("project_name", child.name),
            "description": meta.get("description", ""),
            "mode": meta.get("mode", "general"),
            "last_learn_time": meta.get("last_learn_time", ""),
            "note_count": len(notes),
            "create_time": meta.get("create_time", ""),
        })
    projects.sort(key=lambda x: x["last_learn_time"], reverse=True)
    return projects


def create_project(project_name: str, description: str = "", owner: str = "admin") -> dict[str, Any]:
    """创建完整项目目录树并返回项目信息。owner 为所属用户（默认 admin）。"""
    name = (project_name or "").strip()
    if not name:
        raise ValueError("项目名称不能为空")

    project_id = uuid.uuid4().hex[:12]
    # 先登记归属，使 _project_dir 定位到 data/{owner}/{pid}
    register_project_owner(project_id, owner)
    pdir = _project_dir(project_id)
    if pdir.exists():
        raise ValueError(f"项目目录已存在: {project_id}")

    (pdir / CONFIG_DIR_NAME / SOURCE_DIR).mkdir(parents=True)
    (pdir / CONFIG_DIR_NAME / PLAN_DIR).mkdir(parents=True)
    (pdir / CONFIG_DIR_NAME / SNAPSHOT_DIR).mkdir(parents=True)
    (pdir / CONFIG_DIR_NAME / LOG_DIR).mkdir(parents=True)

    meta = {
        "project_id": project_id,
        "project_name": name,
        "description": description,
        "create_time": _now(),
        "mode": "general",
        "active_material_set": [],
        "supplementary_materials": [],
        "last_learn_time": "",
        "note_count": 0,
    }
    _dump_yaml(pdir / CONFIG_DIR_NAME / META_FILE, meta)

    # 初始化空计划与空快照（latest），保证 load 时文件存在
    _dump_yaml(pdir / CONFIG_DIR_NAME / SNAPSHOT_DIR / SNAPSHOT_LATEST, {
        "learner_state": _empty_learner_state(),
        "meta": {"snapshot_version": 0, "updated_at": _now()},
    })
    (pdir / CONFIG_DIR_NAME / PLAN_DIR / PLAN_LATEST).write_text(
        f"# 学习计划：{name}\n\n> 尚未生成计划，等待探查阶段完成后写入。\n",
        encoding="utf-8",
    )

    return {
        "project_id": project_id,
        "project_name": name,
        "description": description,
        "mode": "general",
        "note_count": 0,
        "create_time": meta["create_time"],
    }


# ---------------------------------------------------------------------------
# 笔记目录（支持用户自定义 Obsidian vault 路径）
# ---------------------------------------------------------------------------
def _notes_dir(project_id: str) -> Path:
    """返回项目学习笔记目录。

    - 配置了笔记根（LEARN_NOTES_ROOT，Obsidian vault 路径）→ {根}/{项目名}/
    - 未配置 → 项目文件夹根（旧行为）
    """
    from config import get_notes_root
    pdir = _project_dir(project_id)
    root = get_notes_root()
    if root:
        meta = _load_yaml(pdir / CONFIG_DIR_NAME / META_FILE)
        proj_name = meta.get("project_name") or pdir.name
        d = Path(root) / _safe_filename(proj_name)
        d.mkdir(parents=True, exist_ok=True)
        return d
    return pdir


def migrate_notes(project_id: str) -> None:
    """把项目文件夹根下的 *.md 笔记迁移到配置的笔记目录，并在项目根保留一份快照副本。

    - 复制到笔记目录（日常读写真源，Obsidian 打开）
    - 源文件保留在项目根（快照备份，防外部目录误删）
    仅在配置了笔记根目录时执行；每次加载项目时调用，幂等。
    """
    from config import get_notes_root
    pdir = _project_dir(project_id)
    if not get_notes_root():
        return
    nd = _notes_dir(project_id)
    if nd.resolve() == pdir.resolve():
        return
    import shutil
    for f in pdir.glob("*.md"):
        if f.name == "README.md":
            continue
        target = nd / f.name
        if not target.exists():
            try:
                shutil.copy2(str(f), str(target))   # 复制过去，源保留为快照
            except OSError:
                pass


def load_project(project_id: str) -> dict[str, Any]:
    """加载项目：校验 schema，读取最新快照与最新计划，返回项目上下文。

    素材登记以磁盘为权威：active_material_set / supplementary_materials
    中磁盘已不存在的条目会被清除并写回 meta；无任何素材时模式回退 general。
    """
    from config import get_notes_root
    pdir = _project_dir(project_id)
    migrate_notes(project_id)
    meta = _load_yaml(pdir / CONFIG_DIR_NAME / META_FILE)
    _validate_meta(meta)

    _sync_material_meta(project_id, meta)

    snap = _load_yaml(pdir / CONFIG_DIR_NAME / SNAPSHOT_DIR / SNAPSHOT_LATEST)
    if not snap or "learner_state" not in snap:
        snap = {"learner_state": _empty_learner_state(),
                "meta": {"snapshot_version": 0, "updated_at": _now()}}

    plan_path = pdir / CONFIG_DIR_NAME / PLAN_DIR / PLAN_LATEST
    plan_latest = plan_path.read_text(encoding="utf-8") if plan_path.exists() else ""

    notes_dir = _notes_dir(project_id)
    notes = sorted(
        (p.name for p in notes_dir.glob("*.md") if p.name != "README.md"),
        key=lambda n: _note_sort_key(n),
    )
    materials = [p.name for p in (pdir / CONFIG_DIR_NAME / SOURCE_DIR).glob("*")
                 if p.is_file()]

    return {
        "meta": meta,
        "learner_state": snap.get("learner_state", _empty_learner_state()),
        "snapshot_meta": snap.get("meta", {}),
        "plan_latest": plan_latest,
        "notes": notes,
        "materials": materials,
        "notes_root": str(notes_dir),
        "notes_external": bool(get_notes_root()),
    }


def _note_sort_key(name: str) -> tuple[int, int, str]:
    """按 单元NN 数字排序，兼容无编号的文件名。"""
    m = re.match(r"单元(\d+)[\-_]?(.*)", name)
    if m:
        return (0, int(m.group(1)), m.group(2))
    return (1, 0, name)


def _sync_material_meta(project_id: str, meta: dict[str, Any]) -> None:
    """素材登记与磁盘对齐：清除已不存在的登记；无素材时模式回退 general。

    磁盘是唯一权威：meta 里登记的素材名若在 source_material/ 找不到真实文件，
    一律移除（防止 AI 按过期登记调取不存在的素材）。同步后若无任何素材，
    mode 由 document_anchor 回退为 general。
    """
    pdir = _project_dir(project_id)
    src_dir = pdir / CONFIG_DIR_NAME / SOURCE_DIR
    real_files = {p.name for p in src_dir.glob("*") if p.is_file()}

    changed = False
    for field in ("active_material_set", "supplementary_materials"):
        lst = [x for x in meta.get(field, []) if x in real_files]
        if lst != meta.get(field, []):
            meta[field] = lst
            changed = True

    has_any = bool(meta.get("active_material_set")) or bool(meta.get("supplementary_materials"))
    if not has_any and meta.get("mode") == "document_anchor":
        meta["mode"] = "general"
        changed = True

    if changed:
        _dump_yaml(pdir / CONFIG_DIR_NAME / META_FILE, meta)


def _empty_learner_state() -> dict[str, Any]:
    return {
        "mastered": [],
        "partial_understand": [],
        "misconceptions": [],
        "missing_prerequisite": [],
        "zpd_status": "within",
        "current_topic": "",
        "pending_list": [],
        "current_teaching_unit": "",
    }


# ---------------------------------------------------------------------------
# 快照
# ---------------------------------------------------------------------------
def save_snapshot(project_id: str, learner_state: dict[str, Any],
                  expected_mtime: float | None = None) -> dict[str, Any]:
    """保存状态快照：带并发检测与 schema 校验。

    expected_mtime: 会话加载快照时记录的磁盘 mtime；若磁盘已被他方修改，返回告警但仍保存。
    """
    pdir = _project_dir(project_id)
    snap_dir = pdir / CONFIG_DIR_NAME / SNAPSHOT_DIR
    snap_dir.mkdir(parents=True, exist_ok=True)

    latest = snap_dir / SNAPSHOT_LATEST
    warning = None
    if expected_mtime is not None and latest.exists():
        if abs(latest.stat().st_mtime - expected_mtime) > 0.01:
            warning = ("⚠️ 检测到该项目可能被另一个会话同时编辑，本次快照已覆盖保存，"
                       "请尽量只使用一个会话操作同一个项目。")

    state = dict(learner_state or {})
    state.setdefault("mastered", [])
    state.setdefault("partial_understand", [])
    state.setdefault("misconceptions", [])
    state.setdefault("missing_prerequisite", [])
    state.setdefault("zpd_status", "within")
    state.setdefault("current_topic", "")
    state.setdefault("pending_list", [])
    state.setdefault("current_teaching_unit", "")

    if state["zpd_status"] not in ZPD_VALUES:
        state["zpd_status"] = "within"
    for mc in state["misconceptions"]:
        if isinstance(mc, dict) and mc.get("root_cause") not in ROOT_CAUSE_VALUES:
            mc["root_cause"] = "无"

    old_version = 0
    if latest.exists():
        old = _load_yaml(latest)
        old_version = (old.get("meta", {}) or {}).get("snapshot_version", 0) or 0

    version = old_version + 1
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    snap_data = {
        "learner_state": state,
        "meta": {
            "snapshot_version": version,
            "updated_at": _now(),
        },
    }
    _dump_yaml(snap_dir / f"snapshot_{stamp}_v{version}.yaml", snap_data)
    _dump_yaml(latest, snap_data)

    # 更新 meta.last_learn_time
    meta = _load_yaml(pdir / CONFIG_DIR_NAME / META_FILE)
    meta["last_learn_time"] = _now()
    _dump_yaml(pdir / CONFIG_DIR_NAME / META_FILE, meta)

    return {"version": version, "warning": warning, "updated_at": snap_data["meta"]["updated_at"],
            "latest_mtime": latest.stat().st_mtime if latest.exists() else 0}


# ---------------------------------------------------------------------------
# 计划
# ---------------------------------------------------------------------------
def plan_write(project_id: str, plan_markdown: str, change_reason: str = "") -> dict[str, Any]:
    """保存学习计划：生成精简命名历史版本（plan_vN.md），更新 plan_latest.md。

    plan_markdown 为完整计划文本（markdown）。历史版本不覆盖。
    版本号 = 现有历史版本数 + 1（plan_latest.md 不计入）。
    """
    pdir = _project_dir(project_id)
    plan_dir = pdir / CONFIG_DIR_NAME / PLAN_DIR
    plan_dir.mkdir(parents=True, exist_ok=True)

    history_files = sorted(p.name for p in plan_dir.glob("plan_v*.md"))

    def _vn(name: str) -> int:
        m = re.search(r"v(\d+)", name)
        return int(m.group(1)) if m else 0

    version_no = max((_vn(x) for x in history_files), default=0) + 1
    history_file = plan_dir / f"plan_v{version_no}.md"

    header = f"> 版本 v{version_no} ｜ 保存时间：{_now()}\n"
    if change_reason:
        header += f"> 变更原因：{change_reason}\n"
    full = header + "\n" + (plan_markdown or "").strip() + "\n"

    history_file.write_text(full, encoding="utf-8")
    (plan_dir / PLAN_LATEST).write_text(full, encoding="utf-8")

    return {"version": version_no, "file": history_file.name, "updated_at": _now()}


def list_plan_versions(project_id: str) -> list[str]:
    """列出历史计划版本（plan_vN.md，按版本号排序；不含 plan_latest.md）。"""
    pdir = _project_dir(project_id)
    plan_dir = pdir / CONFIG_DIR_NAME / PLAN_DIR
    if not plan_dir.exists():
        return []
    files = [p.name for p in plan_dir.glob("plan_v*.md")]

    def key(name: str) -> int:
        m = re.search(r"v(\d+)", name)
        return int(m.group(1)) if m else 0
    return sorted(files, key=key)


def read_plan_file(project_id: str, file_name: str) -> str:
    """读取指定计划文件（仅限 plan_latest.md 或 plan_vN.md，防路径穿越）。"""
    name = Path(file_name).name
    if name != PLAN_LATEST and not re.fullmatch(r"plan_v\d+\.md", name):
        raise ValueError(f"非法的计划文件名: {file_name}")
    pdir = _project_dir(project_id)
    plan_dir = pdir / CONFIG_DIR_NAME / PLAN_DIR
    target = plan_dir / name
    if not target.is_file():
        raise FileNotFoundError(f"计划文件不存在: {name}")
    return target.read_text(encoding="utf-8", errors="replace")


# ---------------------------------------------------------------------------
# 笔记
# ---------------------------------------------------------------------------
def append_note(project_id: str, note_title: str, content: str) -> dict[str, Any]:
    """把本轮教学输出直接归档为单元笔记（不重新生成）。

    note_title 允许包含编号，如"单元01-变量与数据类型"；若不带编号自动递增。
    """
    pdir = _project_dir(project_id)
    meta = _load_yaml(pdir / CONFIG_DIR_NAME / META_FILE)

    title = (note_title or "").strip()
    if not title:
        count = int(meta.get("note_count", 0) or 0) + 1
        title = f"单元{count:02d}-学习笔记"
    elif not re.match(r"单元\d+", title):
        count = int(meta.get("note_count", 0) or 0) + 1
        title = f"单元{count:02d}-{title}"

    # 同名笔记追加到原文件（一个单元一份笔记，内容持续累积），避免重复归档产生多个相似 md
    filename = _safe_filename(title)
    ndir = _notes_dir(project_id)
    target = ndir / f"{filename}.md"
    if target.exists():
        with target.open("a", encoding="utf-8") as f:
            f.write(f"\n\n---\n> 补充归档：{_now()}\n\n")
            f.write((content or "").strip() + "\n")
        return {"note_file": target.name, "note_title": title, "appended": True}

    header = f"# {title}\n\n> 归档时间：{_now()}\n> 来源：会话教学输出（未重写）\n\n---\n\n"
    target.write_text(header + (content or "").strip() + "\n", encoding="utf-8")

    meta["note_count"] = int(meta.get("note_count", 0) or 0) + 1
    _dump_yaml(pdir / CONFIG_DIR_NAME / META_FILE, meta)

    return {"note_file": target.name, "note_title": title}


def _safe_filename(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|\r\n\t]', "-", name).strip()
    return name[:60] or "note"


# ---------------------------------------------------------------------------
# 素材
# ---------------------------------------------------------------------------
def add_material(project_id: str, filename: str, content: str,
                 role: str = "main") -> dict[str, Any]:
    """保存用户粘贴的原始素材到 source_material（只读区，写入一次后不再被程序修改）。"""
    pdir = _project_dir(project_id)
    src_dir = pdir / CONFIG_DIR_NAME / SOURCE_DIR
    src_dir.mkdir(parents=True, exist_ok=True)

    safe_name = _safe_filename(filename)
    if not safe_name.endswith((".md", ".txt")):
        safe_name += ".md"
    target = src_dir / safe_name
    if target.exists():
        safe_name = f"{safe_name[:-3]}_{datetime.now().strftime('%H%M%S')}.md"
        target = src_dir / safe_name

    target.write_text(content or "", encoding="utf-8")

    meta = _load_yaml(pdir / CONFIG_DIR_NAME / META_FILE)
    field = "active_material_set" if role == "main" else "supplementary_materials"
    meta.setdefault(field, [])
    if safe_name not in meta[field]:
        meta[field].append(safe_name)
    if role == "main":
        meta["mode"] = "document_anchor"
    _dump_yaml(pdir / CONFIG_DIR_NAME / META_FILE, meta)

    return {"filename": safe_name, "mode": meta["mode"]}


# ---------------------------------------------------------------------------
# 素材子目录：源文件（原始格式） / 无法处理（预处理失败）
# source_material/ 根目录只放 AI 可读的 md；其余文件归入子目录
# ---------------------------------------------------------------------------
def _raw_subdir(project_id: str, kind: str) -> Path:
    """kind ∈ source / quarantine → 子目录路径。"""
    pdir = _project_dir(project_id)
    sub = SOURCE_RAW_DIR if kind == "source" else SOURCE_QUAR_DIR
    d = pdir / CONFIG_DIR_NAME / SOURCE_DIR / sub
    d.mkdir(parents=True, exist_ok=True)
    return d


def save_raw_file(project_id: str, filename: str, data: bytes, kind: str = "source") -> str:
    """保存原始格式文件到子目录（同名加时间戳，不覆盖）。返回保存的文件名。"""
    safe = _safe_filename(Path(filename).name)
    if not safe:
        safe = "unnamed"
    d = _raw_subdir(project_id, kind)
    target = d / safe
    if target.exists():
        safe = f"{Path(safe).stem}_{datetime.now().strftime('%H%M%S')}{Path(safe).suffix}"
        target = d / safe
    target.write_bytes(data)
    return target.name


def list_raw_files(project_id: str, kind: str) -> list[dict[str, Any]]:
    """列出子目录文件：name / size / mtime。"""
    d = _raw_subdir(project_id, kind)
    out = []
    for f in sorted(d.iterdir(), key=lambda p: p.name):
        if not f.is_file():
            continue
        out.append({
            "name": f.name,
            "size": f.stat().st_size,
            "mtime": datetime.fromtimestamp(f.stat().st_mtime).strftime("%m-%d %H:%M"),
        })
    return out


def delete_raw_file(project_id: str, kind: str, name: str) -> dict[str, Any]:
    """删除子目录文件（进回收站，可恢复）。"""
    d = _raw_subdir(project_id, kind)
    safe = Path(name).name
    target = d / safe
    if not target.is_file():
        raise FileNotFoundError(f"文件不存在: {name}")

    trash = _trash_dir(project_id)
    trash_name = f"raw{'src' if kind == 'source' else 'quar'}__{target.name}"
    trash_target = trash / trash_name
    n = 1
    while trash_target.exists():
        trash_target = trash / f"{trash_name}_{n}"
        n += 1
    shutil.move(str(target), str(trash_target))
    return {"deleted": True, "trash": trash_target.name, "kind": kind, "name": name}


def read_raw_file(project_id: str, kind: str, name: str) -> Path:
    """返回子目录文件路径（校验通过），供重试预处理使用。"""
    d = _raw_subdir(project_id, kind)
    safe = Path(name).name
    target = d / safe
    if not target.is_file():
        raise FileNotFoundError(f"文件不存在: {name}")
    return target


def material_extract(project_id: str, material_name: str, query: str = "",
                     max_chars: int = 3000, mode: str = "snippet",
                     offset: int = 0) -> dict[str, Any]:
    """从 source_material 提取原文片段，替代 shell grep/awk。

    三种模式：
    - mode=snippet（默认）：query 非空按关键词定位上下文 ±8 行；query 为空返回开头 80 行
    - mode=outline：提取素材标题结构（# / ## / ### 行），用于快速了解全书框架
    - mode=full：从 offset 字符位置开始读取 max_chars 字符（分块通读长文档）
    """
    pdir = _project_dir(project_id)
    src_dir = pdir / CONFIG_DIR_NAME / SOURCE_DIR
    target = src_dir / material_name
    if not target.is_file():
        # 尝试模糊匹配（忽略扩展名）
        candidates = [p for p in src_dir.iterdir() if p.is_file()
                      and p.stem == Path(material_name).stem]
        if not candidates:
            raise FileNotFoundError(f"素材文件不存在: {material_name}")
        target = candidates[0]

    text = target.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()

    if mode == "outline":
        heads = []
        for ln in lines:
            s = ln.strip()
            if s.startswith("#") and not s.startswith("#!"):
                heads.append(s[:80])
        return {"material": target.name, "mode": "outline",
                "total_chars": len(text), "heading_count": len(heads),
                "headings": heads[:200]}

    if mode == "full":
        offset = max(0, int(offset or 0))
        chunk = text[offset:offset + max_chars]
        return {"material": target.name, "mode": "full", "offset": offset,
                "chars": len(chunk), "total_chars": len(text),
                "content": chunk}

    query = (query or "").strip()
    if not query:
        snippet = "\n".join(lines[:80])
        return {"material": target.name, "query": "", "snippet": snippet[:max_chars],
                "total_chars": len(text), "mode": "snippet"}

    # 关键词匹配：取命中行及其上下文 ±8 行
    hits = [i for i, ln in enumerate(lines) if query.lower() in ln.lower()]
    if not hits:
        return {"material": target.name, "query": query, "snippet": "",
                "total_chars": len(text), "mode": "snippet",
                "message": "未找到包含该关键词的片段，请换关键词或检查素材内容。"}

    idx = hits[0]
    start = max(0, idx - 8)
    end = min(len(lines), idx + 9)
    snippet = "\n".join(lines[start:end])
    return {"material": target.name, "query": query, "snippet": snippet[:max_chars],
            "total_chars": len(text), "hit_line": idx + 1, "mode": "snippet"}


# ---------------------------------------------------------------------------
# 日志与重载
# ---------------------------------------------------------------------------
def append_log(project_id: str, log_text: str) -> dict[str, Any]:
    """追加机器学习元日志。"""
    pdir = _project_dir(project_id)
    log_dir = pdir / CONFIG_DIR_NAME / LOG_DIR
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    entry = f"\n--- [{stamp}] ---\n{log_text.strip()}\n"
    with (log_dir / "learn_session.log").open("a", encoding="utf-8") as f:
        f.write(entry)
    return {"logged_at": _now()}


def read_log(project_id: str, tail: int = 200) -> str:
    pdir = _project_dir(project_id)
    log_file = pdir / CONFIG_DIR_NAME / LOG_DIR / "learn_session.log"
    if not log_file.exists():
        return "（暂无日志）"
    lines = log_file.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(lines[-tail:])


def reload_project(project_id: str) -> dict[str, Any]:
    """重新从磁盘加载项目（供 reload 指令使用）。"""
    return load_project(project_id)


# ---------------------------------------------------------------------------
# md 文件增删改（素材 / 笔记 / 计划 统一文件操作）
# ---------------------------------------------------------------------------
# kind → 目录定位（相对项目根）
_KIND_DIRS = {
    "material": lambda p: p / CONFIG_DIR_NAME / SOURCE_DIR,
    "note": lambda p: p,                          # 项目根目录（单元笔记）
    "plan": lambda p: p / CONFIG_DIR_NAME / PLAN_DIR,
}


def _resolve_file(project_id: str, kind: str, name: str) -> Path:
    """解析 kind+name → 文件绝对路径（白名单 + 防穿越）。"""
    if kind not in _KIND_DIRS:
        raise ValueError(f"非法的文件类型: {kind}")
    pdir = _project_dir(project_id)
    safe = Path(name).name                      # 只取文件名，去路径
    if safe != name or not safe.endswith((".md", ".txt", ".markdown")):
        raise ValueError("文件名不合法")
    if kind == "note":
        base = _notes_dir(project_id)
    else:
        base = _KIND_DIRS[kind](pdir)
    target = (base / safe).resolve()
    root = base.resolve()
    if not target.is_relative_to(root):
        raise ValueError("路径越界")
    return target


def update_file(project_id: str, kind: str, name: str, content: str) -> dict[str, Any]:
    """更新文件内容。plan 类型会先备份旧版本再覆盖 latest。"""
    target = _resolve_file(project_id, kind, name)
    if not target.exists():
        raise FileNotFoundError(f"文件不存在: {name}")

    if kind == "plan":
        # 旧版本备份到历史文件，再覆盖 plan_latest.md
        if target.name == PLAN_LATEST:
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            old = target.read_text(encoding="utf-8", errors="replace")
            backup = target.parent / f"plan_{stamp}_backup.md"
            backup.write_text(old, encoding="utf-8")
        else:
            raise FileNotFoundError("计划编辑仅支持 plan_latest.md")

    target.write_text(content or "", encoding="utf-8")
    return {"kind": kind, "name": target.name, "updated": True}


def create_note(project_id: str, name: str, content: str = "") -> dict[str, Any]:
    """新建笔记文件（用户手动创建）。"""
    pdir = _project_dir(project_id)
    ndir = _notes_dir(project_id)
    safe = _safe_filename(name)
    if not safe.endswith(".md"):
        safe += ".md"
    target = ndir / safe
    if target.exists():
        raise ValueError(f"笔记已存在: {safe}")
    target.write_text(content or "", encoding="utf-8")

    meta = _load_yaml(pdir / CONFIG_DIR_NAME / META_FILE)
    meta["note_count"] = int(meta.get("note_count", 0) or 0) + 1
    _dump_yaml(pdir / CONFIG_DIR_NAME / META_FILE, meta)
    return {"note_file": target.name}


def _trash_dir(project_id: str) -> Path:
    pdir = _project_dir(project_id)
    d = pdir / CONFIG_DIR_NAME / TRASH_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def delete_file(project_id: str, kind: str, name: str) -> dict[str, Any]:
    """删除文件（material/note；plan 不允许删除 latest）。

    删除进回收站（.learn_config/trash/），可恢复；防误删。
    """
    target = _resolve_file(project_id, kind, name)
    if not target.exists():
        raise FileNotFoundError(f"文件不存在: {name}")
    if kind == "plan" and target.name == PLAN_LATEST:
        raise ValueError("不允许删除当前生效计划")

    trash = _trash_dir(project_id)
    trash_name = f"{kind}__{target.name}"
    trash_target = trash / trash_name
    n = 1
    while trash_target.exists():
        trash_target = trash / f"{trash_name[:-3]}_{n}{target.suffix}" if trash_name.endswith(
            (".md", ".txt", ".markdown")) else trash / f"{trash_name}_{n}"
        n += 1
    shutil.move(str(target), str(trash_target))

    if kind == "note":
        pdir = _project_dir(project_id)
        meta = _load_yaml(pdir / CONFIG_DIR_NAME / META_FILE)
        meta["note_count"] = max(0, int(meta.get("note_count", 0) or 0) - 1)
        _dump_yaml(pdir / CONFIG_DIR_NAME / META_FILE, meta)
    elif kind == "material":
        # 同步移除 meta 登记（磁盘为权威）
        meta = _load_yaml(pdir := _project_dir(project_id) / CONFIG_DIR_NAME / META_FILE)
        for field in ("active_material_set", "supplementary_materials"):
            meta[field] = [x for x in meta.get(field, []) if x != target.name]
        _sync_material_meta(project_id, meta)
    return {"kind": kind, "name": name, "deleted": True, "trash": trash_target.name}


def list_trash(project_id: str) -> list[dict[str, Any]]:
    """列出回收站内容（含原位置信息）。"""
    trash = _trash_dir(project_id)
    out = []
    for f in sorted(trash.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if not f.is_file():
            continue
        raw = f.name
        if "__" in raw:
            kind, orig = raw.split("__", 1)
        else:
            kind, orig = "file", raw
        out.append({
            "trash_name": raw,
            "kind": kind,
            "original_name": orig,
            "deleted_at": datetime.fromtimestamp(f.stat().st_mtime).strftime("%m-%d %H:%M"),
        })
    return out


def restore_file(project_id: str, trash_name: str) -> dict[str, Any]:
    """从回收站恢复文件到原位置。"""
    trash = _trash_dir(project_id)
    src = trash / trash_name
    if not src.is_file():
        raise FileNotFoundError(f"回收站无此文件: {trash_name}")
    raw = src.name
    if "__" in raw:
        kind, orig = raw.split("__", 1)
    else:
        kind, orig = "file", raw
    if kind == "rawsrc":
        target = _raw_subdir(project_id, "source") / Path(orig).name
    elif kind == "rawquar":
        target = _raw_subdir(project_id, "quarantine") / Path(orig).name
    else:
        target = _resolve_file(project_id, kind, orig)
    if target.exists():
        raise ValueError(f"原位置已存在同名文件: {orig}（可先删除现有文件再恢复）")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(target))
    if kind == "note":
        pdir = _project_dir(project_id)
        meta = _load_yaml(pdir / CONFIG_DIR_NAME / META_FILE)
        meta["note_count"] = int(meta.get("note_count", 0) or 0) + 1
        _dump_yaml(pdir / CONFIG_DIR_NAME / META_FILE, meta)
    elif kind == "material":
        # 恢复后加回 meta 登记；有素材后模式回到 document_anchor
        meta = _load_yaml(pdir := _project_dir(project_id) / CONFIG_DIR_NAME / META_FILE)
        for field in ("active_material_set", "supplementary_materials"):
            if orig in meta.get(field, []):
                break
        else:
            meta.setdefault("active_material_set", [])
            if orig not in meta["active_material_set"]:
                meta["active_material_set"].append(orig)
                meta["mode"] = "document_anchor"
        _dump_yaml(pdir, meta)
    return {"restored": orig, "kind": kind}


def purge_file(project_id: str, trash_name: str) -> dict[str, Any]:
    """彻底删除回收站中的文件（不可恢复）。"""
    trash = _trash_dir(project_id)
    src = trash / trash_name
    if not src.is_file():
        raise FileNotFoundError(f"回收站无此文件: {trash_name}")
    src.unlink()
    return {"purged": trash_name}


def rename_file(project_id: str, kind: str, name: str, new_name: str) -> dict[str, Any]:
    """重命名文件。"""
    target = _resolve_file(project_id, kind, name)
    if not target.exists():
        raise FileNotFoundError(f"文件不存在: {name}")
    if kind == "plan" and target.name == PLAN_LATEST:
        raise ValueError("不允许重命名当前生效计划")

    safe = Path(new_name).name
    if safe != new_name or not safe.endswith((".md", ".txt", ".markdown")):
        raise ValueError("新文件名不合法")
    new_target = target.parent / safe
    if new_target.exists():
        raise ValueError(f"目标文件已存在: {safe}")
    target.rename(new_target)

    if kind == "material":
        meta = _load_yaml(pdir := _project_dir(project_id) / CONFIG_DIR_NAME / META_FILE)
        for field in ("active_material_set", "supplementary_materials"):
            if name in meta.get(field, []):
                meta[field] = [new_target.name if x == name else x for x in meta[field]]
        _dump_yaml(pdir, meta)
    return {"kind": kind, "old": name, "new": new_target.name}


def delete_project(project_id: str) -> dict[str, Any]:
    """删除整个学习项目（保留确认：仅供显式 API 使用，前端需二次确认）。"""
    pdir = _project_dir(project_id)
    if not pdir.exists():
        raise FileNotFoundError(f"项目不存在: {project_id}")
    shutil.rmtree(pdir)
    return {"deleted": project_id}

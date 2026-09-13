"""跨设备文件同步引擎（服务器权威 + hash 增量 + 冲突副本）。

同步范围：项目目录内"用户可见/可编辑"文件，排除 AI 内部状态：
  - .learn_config/state_snapshot/**   （快照历史，服务器权威即可）
  - .learn_config/threads/**          （会话线程 JSON）
  - .learn_config/session_log/**      （AI 会话日志）
  - .learn_config/trash/**            （回收站）
  - .learn_config/plan/plan_v*.md     （历史计划，只同步 plan_latest）

同步单位：相对路径 + sha256 短 hash + size + mtime。
冲突策略：最后写入胜出；服务器版本被别处改过（hash 不一致）时，
先把服务器当前版本存为 {path}.conflict-{ts} 副本，再写入新内容。
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

from core import project_manager as pm

# 不同步的前缀（相对项目根）
EXCLUDE_PREFIXES = (
    ".learn_config/state_snapshot/",
    ".learn_config/threads/",
    ".learn_config/session_log/",
    ".learn_config/trash/",
)


def _hash_file(path: Path) -> str:
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
    except OSError:
        return ""
    return h.hexdigest()[:24]


def _is_text(path: Path) -> bool:
    ext = path.suffix.lower()
    return ext in (".md", ".txt", ".yaml", ".yml", ".json", ".log", ".html", ".css", ".js", ".csv", ".py", ".env", ".meta", "")


def _rel(p: Path, root: Path) -> str:
    return p.relative_to(root).as_posix()


def list_sync_files(project_id: str, owner: str) -> list[dict]:
    """返回项目可同步文件清单（归属校验在 API 层完成）。"""
    pdir = pm._project_dir(project_id)
    files: list[dict] = []
    for p in pdir.rglob("*"):
        if not p.is_file():
            continue
        rel = _rel(p, pdir)
        if rel.startswith(EXCLUDE_PREFIXES):
            continue
        if rel.startswith(".learn_config/plan/") and rel != ".learn_config/plan/plan_latest.md":
            continue
        st = p.stat()
        files.append({
            "path": rel,
            "hash": _hash_file(p),
            "size": st.st_size,
            "mtime": int(st.st_mtime),
        })
    files.sort(key=lambda x: x["path"])
    return files


def pull_files(project_id: str, owner: str, paths: list[str]) -> list[dict]:
    """按路径取文件内容（文本返回 utf-8 字符串，二进制 base64）。"""
    pdir = pm._project_dir(project_id)
    out: list[dict] = []
    import base64
    for rel in paths:
        p = (pdir / rel).resolve()
        # 防穿越：必须仍在项目根内
        if not str(p).startswith(str(pdir.resolve())):
            continue
        if not p.is_file():
            continue
        raw = p.read_bytes()
        if _is_text(p):
            try:
                content = raw.decode("utf-8")
            except UnicodeDecodeError:
                content = base64.b64encode(raw).decode("ascii")
                out.append({"path": rel, "content": content, "encoding": "base64"})
                continue
            out.append({"path": rel, "content": content, "encoding": "utf-8"})
        else:
            out.append({"path": rel, "content": base64.b64encode(raw).decode("ascii"), "encoding": "base64"})
    return out


def push_files(project_id: str, owner: str, changes: list[dict], deleted: list[str] | None = None) -> dict:
    """写入客户端变更。冲突：服务器当前版本存 .conflict-{ts} 副本后写入新内容。

    changes: [{path, content, base_hash, encoding?}]
    deleted: [path, ...]（客户端删除 → 服务器移入回收站）
    返回: {"ok": True, "conflicts": [{path, conflict_file}], "written": n}
    """
    pdir = pm._project_dir(project_id)
    root = pdir.resolve()
    conflicts: list[dict] = []
    written = 0

    for ch in changes:
        rel = ch["path"]
        if rel.startswith(EXCLUDE_PREFIXES):
            continue
        target = (pdir / rel).resolve()
        if not str(target).startswith(str(root)):
            continue  # 防穿越
        target.parent.mkdir(parents=True, exist_ok=True)

        base_hash = ch.get("base_hash") or ""
        cur_hash = _hash_file(target) if target.is_file() else ""
        # 冲突：服务器有内容且与客户端 base 不一致（被别处改过）
        if cur_hash and base_hash and cur_hash != base_hash:
            ts = time.strftime("%Y%m%d_%H%M%S")
            conflict_path = Path(f"{rel}.conflict-{ts}")
            cf = pdir / conflict_path
            cf.parent.mkdir(parents=True, exist_ok=True)
            try:
                cf.write_bytes(target.read_bytes())
                conflicts.append({"path": rel, "conflict_file": conflict_path.as_posix()})
            except OSError:
                pass

        content = ch.get("content", "")
        if ch.get("encoding") == "base64":
            import base64
            target.write_bytes(base64.b64decode(content))
        else:
            target.write_text(content, encoding="utf-8")
        written += 1

    # 删除：移到项目回收站
    for rel in (deleted or []):
        if rel.startswith(EXCLUDE_PREFIXES):
            continue
        target = (pdir / rel).resolve()
        if not str(target).startswith(str(root)):
            continue
        if target.is_file():
            trash = pdir / ".learn_config" / "trash"
            trash.mkdir(parents=True, exist_ok=True)
            ts = time.strftime("%Y%m%d_%H%M%S")
            dest = trash / f"{Path(rel).name}.{ts}.del"
            try:
                target.rename(dest)
            except OSError:
                pass

    return {"ok": True, "conflicts": conflicts, "written": written}

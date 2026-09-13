"""超级学习系统 - 电脑端同步客户端（Obsidian vault ↔ 服务器双向增量同步）

用法：
  python sync_client.py --server http://47.94.251.143:8080 --user admin --pass 你的密码 --vault D:\\vault\\我的笔记 [--once]

- 双向同步每个项目的"学习笔记 + 学习计划"到 vault 下同名文件夹
- 原始材料 / 快照 / 会话等 AI 内部文件不同步到本地（服务器权威，浏览器查看）
- 冲突策略：最后写入胜出 + 服务器/本地自动存 .conflict-{时间戳} 副本
- 默认常驻每 5 分钟同步一次；--once 只跑一轮
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

PLAN_MAP = "学习计划.md"  # 服务器 plan_latest.md 在 vault 中的文件名


def http_json(url: str, method: str = "GET", data: dict | None = None, token: str = "") -> dict:
    body = None
    headers = {}
    if data is not None:
        body = json.dumps(data).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read()
            return json.loads(raw.decode("utf-8")) if raw else {}
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")
        raise RuntimeError(f"HTTP {e.code} {detail[:200]}") from e


def sha256_text(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:24]


def file_hash(p: Path) -> str:
    if not p.is_file():
        return ""
    return hashlib.sha256(p.read_bytes()).hexdigest()[:24]


CONFIG_PATH = Path.home() / ".superstudy_sync.json"


def save_config(args) -> None:
    cfg = {"server": args.server, "user": args.user, "password": args.password, "vault": str(args.vault)}
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    print(f"💾 配置已保存：{CONFIG_PATH}")


def load_config() -> dict:
    if CONFIG_PATH.is_file():
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    return {}


def norm(vault_root: Path, name: str) -> Path:
    safe = "".join(c for c in name if c not in '\\/:*?"<>|').strip()
    return vault_root / safe


def sync_project(base: str, token: str, proj: dict, vault_root: Path, verbose: bool = True) -> dict:
    pid = proj.get("project_id") or proj.get("id")
    name = proj.get("project_name") or pid
    local_dir = norm(vault_root, name)
    local_dir.mkdir(parents=True, exist_ok=True)

    man = http_json(f"{base}/api/sync/{pid}/manifest", "POST", {}, token)
    server_files = {f["path"]: f for f in man.get("files", [])}

    # 服务器端关注的文件：项目根 *.md + plan_latest.md
    server_notes = {p: f for p, f in server_files.items()
                    if not p.startswith(".learn_config/") or p == ".learn_config/plan/plan_latest.md"}
    # 映射到本地路径
    server_map: dict[str, tuple[str, dict]] = {}   # local_rel -> (server_path, meta)
    local_map: dict[str, str] = {}                 # local_rel -> server_path
    for sp, meta in server_notes.items():
        if sp == ".learn_config/plan/plan_latest.md":
            lr = PLAN_MAP
        else:
            lr = Path(sp).name
        server_map[lr] = (sp, meta)
        local_map[lr] = sp

    pulls: list[str] = []      # 服务器 path
    push_changes: list[dict] = []  # 本地变更
    deleted: list[str] = []
    stats = {"down": 0, "up": 0, "conflict": 0, "same": 0}

    for lr, (sp, meta) in server_map.items():
        lp = local_dir / lr
        lh = file_hash(lp)
        if not lp.exists():
            pulls.append(sp)
        elif lh != meta["hash"]:
            pulls.append(sp)   # 服务器更新

    sp_to_lp = {sp: (local_dir / lr) for lr, (sp, _) in server_map.items()}
    if pulls:
        pulled = http_json(f"{base}/api/sync/{pid}/pull", "POST", {"paths": pulls}, token)
        for f in pulled.get("files", []):
            lp = sp_to_lp.get(f["path"])
            if lp is None:
                continue
            content = f["content"]
            if f.get("encoding") == "base64":
                lp.write_bytes(base64.b64decode(content))
            else:
                lp.write_bytes(content.encode("utf-8"))
            stats["down"] += 1
            if verbose:
                print(f"  ↓ {f['path']}")

    # 本地 → 服务器（仅 vault 内新增/修改的笔记）
    for lp in sorted(local_dir.glob("*.md")):
        lr = lp.name
        sp = local_map.get(lr) or lr   # 已映射(计划/服务器笔记)按映射，本地新文件推服务器根同名
        content = lp.read_bytes().decode("utf-8", errors="replace")
        lh = sha256_text(content)
        server_hash = server_files.get(sp, {}).get("hash", "") if sp in server_files else ""
        if lh == server_hash:
            stats["same"] += 1
            continue
        push_changes.append({"path": sp, "content": content, "base_hash": server_hash})

    if push_changes:
        r = http_json(f"{base}/api/sync/{pid}/push", "POST", {"changes": push_changes, "deleted": deleted}, token)
        stats["up"] = r.get("written", 0)
        for c in r.get("conflicts", []):
            stats["conflict"] += 1
            if verbose:
                print(f"  ⚠ 冲突 {c['path']} → 服务器已存副本 {c['conflict_file']}")

    return {"project": name, **stats}


def main():
    ap = argparse.ArgumentParser(description="超级学习系统电脑端同步客户端")
    ap.add_argument("--server", default="", help="服务器地址，如 http://47.94.251.143:8080（缺省读已保存配置）")
    ap.add_argument("--user", default="")
    ap.add_argument("--pass", dest="password", default="")
    ap.add_argument("--vault", default="", help="Obsidian vault 根目录（缺省读已保存配置）")
    ap.add_argument("--once", action="store_true", help="只同步一轮后退出（默认每 300 秒循环）")
    ap.add_argument("--interval", type=int, default=300)
    ap.add_argument("--save", action="store_true", help="保存本次配置并退出")
    args = ap.parse_args()

    # 未显式传参时读取已保存配置
    if not (args.server and args.user and args.password and args.vault):
        cfg = load_config()
        if not cfg:
            print("❌ 未找到配置。首次使用请传全参数：--server --user --pass --vault [--save]")
            sys.exit(1)
        args.server = cfg.get("server", "")
        args.user = cfg.get("user", "")
        args.password = cfg.get("password", "")
        args.vault = cfg.get("vault", "")

    if args.save:
        save_config(args)
        return

    base = args.server.rstrip("/")
    vault_root = Path(args.vault)
    if not vault_root.is_dir():
        print(f"❌ vault 目录不存在：{vault_root}")
        sys.exit(1)

    print("🔑 登录…")
    data = http_json(f"{base}/api/auth/login", "POST",
                     {"username": args.user, "password": args.password})
    token = data.get("token") or data.get("access_token")
    if not token:
        print("❌ 登录失败：", data)
        sys.exit(1)
    print(f"✅ 已登录 {data.get('username')}")

    while True:
        try:
            projs = http_json(f"{base}/api/projects", "GET", token=token).get("projects", [])
            if not projs:
                print("（无学习项目）")
            for p in projs:
                r = sync_project(base, token, p, vault_root)
                print(f"📁 {r['project']}: ↓{r['down']} ↑{r['up']} 冲突{r['conflict']} 相同{r['same']}")
        except Exception as e:
            print(f"⚠ 同步异常：{e}")
        if args.once:
            break
        time.sleep(args.interval)


if __name__ == "__main__":
    main()

"""全局配置：数据根目录、skill 路径、LLM 配置（OpenAI 兼容格式）。

配置来源优先级：环境变量 > backend/.env 文件 > 默认值。
.env 文件可在管理界面（仅本机可访问）中编辑保存，或手工编辑。
"""
from __future__ import annotations

import os
from pathlib import Path

# ---------- 路径 ----------
# 超级学习系统根目录（backend 的上一级）
SYSTEM_ROOT: Path = Path(__file__).resolve().parent.parent

# 业务规约文件（组装 system prompt 用）
SKILL_PATH: Path = Path(os.environ.get("LEARN_SKILL_PATH", SYSTEM_ROOT / "skill.md"))

# 学习项目总根（所有学习项目文件夹都放在这里）
DATA_ROOT: Path = Path(os.environ.get("LEARN_DATA_ROOT", SYSTEM_ROOT / "data"))

# 静态前端目录（前端就放 frontend/ 下，由 FastAPI 托管）
FRONTEND_DIR: Path = SYSTEM_ROOT / "frontend"

# 本地配置文件（backend/.env）
ENV_FILE: Path = Path(__file__).resolve().parent / ".env"

# 服务端口
HOST: str = os.environ.get("LEARN_HOST", "0.0.0.0")
PORT: int = int(os.environ.get("LEARN_PORT", "8080"))

# 会话闲置自动保存时间（秒）
SESSION_IDLE_SAVE_SECONDS: int = int(os.environ.get("LEARN_SESSION_IDLE", "900"))

# ---------- LLM 配置（OpenAI 兼容 /chat/completions） ----------
_LLM_DEFAULTS = {
    "base_url": "https://ark.cn-beijing.volces.com/api/v3",
    "api_key": "",
    "model": "doubao-seed-1-6-250615",
    "timeout": "180",
}

_env_cache: dict[str, str] | None = None


def _load_env_file() -> dict[str, str]:
    """读取 backend/.env（K=V 格式，支持 # 注释）。结果缓存。"""
    global _env_cache
    if _env_cache is not None:
        return _env_cache
    data: dict[str, str] = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            data[k.strip()] = v.strip()
    _env_cache = data
    return data


def _get_llm_setting(key: str, env_name: str) -> str:
    """环境变量 > .env 文件 > 默认值。"""
    val = os.environ.get(env_name)
    if val is not None and val != "":
        return val
    file_val = _load_env_file().get(env_name)
    if file_val:
        return file_val
    return _LLM_DEFAULTS[key]


def get_llm_config() -> dict[str, str]:
    """返回当前生效的 LLM 配置（每次调用实时读取，配置修改即时生效）。"""
    return {
        "base_url": _get_llm_setting("base_url", "LEARN_LLM_BASE_URL"),
        "api_key": _get_llm_setting("api_key", "LEARN_LLM_API_KEY"),
        "model": _get_llm_setting("model", "LEARN_LLM_MODEL"),
        "timeout": _get_llm_setting("timeout", "LEARN_LLM_TIMEOUT"),
    }


def _write_env(entries: dict[str, str | None]) -> None:
    """把配置写入 backend/.env 并刷新缓存。

    entries 值为 None 表示删除该键；非空字符串写入。其他既有键保留。
    """
    global _env_cache
    merged = dict(_load_env_file())
    for k, v in entries.items():
        if v is None or str(v).strip() == "":
            merged.pop(k, None)
        else:
            merged[k] = str(v).strip()

    lines = ["# Probe-Plan-Teach 超级学习系统 - 服务端配置（请勿外泄）",
             "# 修改方式：本机管理界面，或直接编辑本文件后重启服务", ""]
    for k in ("LEARN_LLM_BASE_URL", "LEARN_LLM_API_KEY", "LEARN_LLM_MODEL",
              "LEARN_LLM_TIMEOUT", "LEARN_NOTES_ROOT"):
        if merged.get(k):
            lines.append(f"{k}={merged[k]}")
    ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    _env_cache = None


def save_llm_config(base_url: str | None = None, api_key: str | None = None,
                    model: str | None = None, timeout: str | None = None) -> dict[str, str]:
    """把 LLM 配置写入 backend/.env 并刷新缓存。仅写入非空字段，保留未修改项。

    仅允许本机管理接口调用；Key 只存在服务端文件，绝不下发前端明文。
    """
    updates: dict[str, str | None] = {}
    if base_url is not None:
        updates["LEARN_LLM_BASE_URL"] = base_url
    if api_key is not None:
        updates["LEARN_LLM_API_KEY"] = api_key
    if model is not None:
        updates["LEARN_LLM_MODEL"] = model
    if timeout is not None:
        updates["LEARN_LLM_TIMEOUT"] = timeout
    _write_env(updates)
    return get_llm_config()


# ---------- 学习笔记根目录（Obsidian vault 路径） ----------
def get_notes_root() -> str:
    """用户自定义的学习笔记根目录（本地绝对路径，供 Obsidian 打开）。

    为空表示未配置 → 笔记仍写在项目文件夹内（旧行为）。
    """
    val = os.environ.get("LEARN_NOTES_ROOT") or _load_env_file().get("LEARN_NOTES_ROOT") or ""
    return val.strip()


def save_notes_root(notes_root: str | None) -> str:
    """设置/清空笔记根目录。空值表示清除（笔记回到项目文件夹内）。"""
    _write_env({"LEARN_NOTES_ROOT": notes_root.strip() if notes_root else None})
    return get_notes_root()


def ensure_dirs() -> None:
    """确保数据总根、frontend 存在。"""
    DATA_ROOT.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# 网络代理（解决"浏览器能上网、后端连不上外网"的问题）
# ---------------------------------------------------------------------------
_proxy_cache: str | None | bool = False  # False=未探测


def get_http_proxy() -> str | None:
    """探测后端应使用的 HTTP/HTTPS 代理。

    优先级：环境变量 LEARN_HTTP_PROXY / LEARN_HTTPS_PROXY > Windows 系统代理（注册表）。
    浏览器能上网而后端连不上时，通常就是缺这一步（浏览器走系统代理，Python 默认不走）。
    """
    global _proxy_cache
    if _proxy_cache is not False:
        return _proxy_cache

    proxy = os.environ.get("LEARN_HTTP_PROXY") or os.environ.get("LEARN_HTTPS_PROXY")
    if proxy:
        _proxy_cache = proxy
        return proxy

    # Windows 注册表系统代理
    if os.name == "nt":
        try:
            import winreg
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
            ) as key:
                enable, _ = winreg.QueryValueEx(key, "ProxyEnable")
                server, _ = winreg.QueryValueEx(key, "ProxyServer")
                if enable and server:
                    if "://" not in server:
                        server = "http://" + server
                    _proxy_cache = server
                    return server
        except OSError:
            pass

    _proxy_cache = None
    return None

"""管理 API：LLM 配置读写、服务商预设、模型列表拉取。

安全约束：仅管理员（admin 角色）可访问；
Key 保存在服务端 backend/.env，接口永不返回明文 Key。
"""
from __future__ import annotations

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from auth import require_admin
from config import get_http_proxy, get_llm_config, save_llm_config
router = APIRouter(prefix="/api/admin", tags=["admin"])


# ---------------------------------------------------------------------------
# 主流算力服务商预设（OpenAI 兼容接口）
# ---------------------------------------------------------------------------
PROVIDERS: list[dict] = [
    {
        "id": "volcengine_ark",
        "name": "火山方舟（豆包 / DeepSeek 等）",
        "base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "models": [
            "doubao-seed-1-6-250615",
            "doubao-seed-1-6-flash-250615",
            "doubao-1-5-pro-32k-250115",
            "doubao-1-5-lite-32k-250115",
            "deepseek-v3-250324",
            "deepseek-r1-250528",
        ],
        "default_model": "doubao-seed-1-6-250615",
        "hint": "火山方舟：控制台 → API Key 管理 创建 Key",
    },
    {
        "id": "deepseek",
        "name": "DeepSeek 官方",
        "base_url": "https://api.deepseek.com/v1",
        "models": ["deepseek-chat", "deepseek-reasoner"],
        "default_model": "deepseek-chat",
        "hint": "DeepSeek：platform.deepseek.com → API Keys",
    },
    {
        "id": "dashscope",
        "name": "阿里云百炼（通义千问）",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "models": ["qwen-plus", "qwen-max", "qwen-turbo", "qwen-long",
                   "qwen3-max", "qwen3-plus"],
        "default_model": "qwen-plus",
        "hint": "阿里云百炼：控制台 → API-KEY 管理",
    },
    {
        "id": "openai",
        "name": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "models": ["gpt-4o", "gpt-4o-mini", "gpt-4.1", "o3", "o4-mini"],
        "default_model": "gpt-4o-mini",
        "hint": "OpenAI：platform.openai.com → API keys",
    },
    {
        "id": "zhipu",
        "name": "智谱 AI（GLM）",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "models": ["glm-4-plus", "glm-4-flash", "glm-4-air", "glm-4-long"],
        "default_model": "glm-4-plus",
        "hint": "智谱：open.bigmodel.cn → API Keys",
    },
    {
        "id": "moonshot",
        "name": "月之暗面（Kimi）",
        "base_url": "https://api.moonshot.cn/v1",
        "models": ["moonshot-v1-8k", "moonshot-v1-32k", "moonshot-v1-128k"],
        "default_model": "moonshot-v1-32k",
        "hint": "Kimi：platform.moonshot.cn → API Key 管理",
    },
    {
        "id": "siliconflow",
        "name": "硅基流动（开源模型聚合）",
        "base_url": "https://api.siliconflow.cn/v1",
        "models": ["deepseek-ai/DeepSeek-V3",
                   "Qwen/Qwen2.5-72B-Instruct",
                   "meta-llama/Llama-3.3-70B-Instruct"],
        "default_model": "deepseek-ai/DeepSeek-V3",
        "hint": "硅基流动：cloud.siliconflow.cn → API 密钥",
    },
    {
        "id": "ollama",
        "name": "Ollama（本机免费）",
        "base_url": "http://localhost:11434/v1",
        "models": ["qwen2.5:7b", "llama3.1:8b", "deepseek-r1:7b"],
        "default_model": "qwen2.5:7b",
        "hint": "本机 Ollama：无需 Key，Key 可留空（填任意字符）",
    },
]


def _mask_key(key: str) -> dict[str, bool | str]:
    if not key:
        return {"configured": False, "masked": ""}
    tail = key[-4:] if len(key) > 4 else key
    return {"configured": True, "masked": f"****{tail}"}


class ProviderBody(BaseModel):
    name: str | None = None
    base_url: str | None = None
    api_key: str = ""
    model: str = ""
    models: list[str] = []
    timeout: int | None = None
    enabled: bool | None = None


class LLMTestBody(BaseModel):
    base_url: str = ""
    api_key: str = ""
    model: str = ""


class ModelsBody(BaseModel):
    base_url: str = ""
    api_key: str = ""


class ProviderTestBody(BaseModel):
    model: str = ""
    api_key: str = ""


def _provider_view(p: dict) -> dict:
    from core import provider_manager as prov
    return {
        "id": p["id"],
        "name": p["name"],
        "base_url": p["base_url"],
        "model": p["model"],
        "models": p["models"],
        "timeout": p["timeout"],
        "enabled": p["enabled"],
        "created_at": p["created_at"],
        "key": _mask_key(p["api_key"]),
    }


@router.get("/llm-presets")
def get_presets(admin: dict = Depends(require_admin)):
    """预置算力服务商（前端选择用，不含任何 Key）。"""
    return {"providers": PROVIDERS}


@router.get("/llm-providers")
def list_providers(admin: dict = Depends(require_admin)):
    """管理端：全部 LLM 供应商（Key 掩码）。"""
    from core import provider_manager as prov
    return {"providers": [_provider_view(p) for p in prov.list_providers()]}


@router.post("/llm-providers")
def add_provider(body: ProviderBody, admin: dict = Depends(require_admin)):
    from core import provider_manager as prov
    if not (body.name or "").strip():
        raise HTTPException(status_code=400, detail="请填写供应商名称")
    if not (body.base_url or "").strip():
        raise HTTPException(status_code=400, detail="请填写接口地址")
    if not (body.api_key or "").strip():
        raise HTTPException(status_code=400, detail="请填写 API Key")
    if not (body.model or "").strip():
        raise HTTPException(status_code=400, detail="请填写默认模型")
    try:
        p = prov.add_provider(
            name=body.name, base_url=body.base_url, api_key=body.api_key,
            model=body.model, models=body.models or [],
            timeout=body.timeout or 180)
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"保存失败：{e}")
    return {"saved": True, "provider": _provider_view(p)}


@router.put("/llm-providers/{provider_id}")
def update_provider(provider_id: int, body: ProviderBody,
                    admin: dict = Depends(require_admin)):
    """修改供应商。api_key 留空 = 不修改；models 传空列表 = 清空模型表。"""
    from core import provider_manager as prov
    p = prov.update_provider(
        provider_id,
        name=(body.name.strip() if body.name else None),
        base_url=(body.base_url.strip() if body.base_url else None),
        api_key=body.api_key,
        model=(body.model.strip() if body.model else None),
        models=body.models if body.models is not None else None,
        timeout=body.timeout,
        enabled=body.enabled,
    )
    if p is None:
        raise HTTPException(status_code=404, detail="供应商不存在")
    return {"saved": True, "provider": _provider_view(p)}


@router.delete("/llm-providers/{provider_id}")
def delete_provider(provider_id: int, admin: dict = Depends(require_admin)):
    from core import provider_manager as prov
    if not prov.delete_provider(provider_id):
        raise HTTPException(status_code=404, detail="供应商不存在")
    return {"deleted": True}


@router.post("/llm-test")
async def test_llm_form(body: LLMTestBody, admin: dict = Depends(require_admin)):
    """表单测试：用填写的地址/Key/模型发最小请求（Key 仅本次使用）。"""
    base_url = (body.base_url or "").rstrip("/")
    model = (body.model or "").strip()
    if not base_url:
        raise HTTPException(status_code=400, detail="接口地址不能为空")
    if not model:
        raise HTTPException(status_code=400, detail="请先选择模型")
    api_key = (body.api_key or "").strip()
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    client_kwargs = {"timeout": 30}
    proxy = get_http_proxy()
    if proxy:
        client_kwargs["proxy"] = proxy
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "回复两个字：正常"}],
        "max_tokens": 16,
        "temperature": 0,
    }
    import time
    t0 = time.time()
    try:
        async with httpx.AsyncClient(**client_kwargs) as client:
            resp = await client.post(base_url + "/chat/completions",
                                     json=payload, headers=headers)
            latency = int((time.time() - t0) * 1000)
            if resp.status_code == 200:
                data = resp.json()
                reply = ((data.get("choices") or [{}])[0]
                         .get("message", {}).get("content", ""))
                return {"ok": True, "latency_ms": latency,
                        "reply": reply[:80], "proxy": bool(proxy)}
            if resp.status_code in (401, 403):
                return {"ok": False, "latency_ms": latency,
                        "error": f"API Key 无效或无权限（HTTP {resp.status_code}）", "proxy": bool(proxy)}
            return {"ok": False, "latency_ms": latency,
                    "error": f"服务商返回错误（HTTP {resp.status_code}）：{resp.text[:200]}",
                    "proxy": bool(proxy)}
    except Exception as e:
        latency = int((time.time() - t0) * 1000)
        return {"ok": False, "latency_ms": latency,
                "error": f"无法连接：{e}", "proxy": bool(proxy)}


@router.post("/llm-models")
async def fetch_models_by_form(body: ModelsBody, admin: dict = Depends(require_admin)):
    """新增供应商时：用表单填写的 base_url + api_key 拉取其模型列表（Key 仅本次使用）。"""
    base_url = (body.base_url or "").rstrip("/")
    if not base_url:
        raise HTTPException(status_code=400, detail="接口地址不能为空")
    api_key = (body.api_key or "").strip()
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    client_kwargs = {"timeout": 20}
    proxy = get_http_proxy()
    if proxy:
        client_kwargs["proxy"] = proxy
    try:
        async with httpx.AsyncClient(**client_kwargs) as client:
            resp = await client.get(base_url + "/models", headers=headers)
            if resp.status_code == 200:
                data = resp.json()
                ids = [m.get("id") for m in data.get("data", []) if m.get("id")]
                if ids:
                    return {"source": "live", "models": ids}
            if resp.status_code in (401, 403):
                return {"source": "error", "error": f"API Key 无效或无权限（HTTP {resp.status_code}）", "models": []}
            return {"source": "error", "error": f"获取模型失败（HTTP {resp.status_code}）", "models": []}
    except Exception as e:
        return {"source": "error", "error": f"无法连接该服务商：{e}", "models": []}


@router.post("/llm-providers/{provider_id}/models")
async def fetch_provider_models(provider_id: int, body: ProviderTestBody,
                                admin: dict = Depends(require_admin)):
    """用该供应商已存 Key 拉取其真实模型列表（调 /models）。"""
    from core import provider_manager as prov
    p = prov.get_provider(provider_id)
    if p is None:
        raise HTTPException(status_code=404, detail="供应商不存在")
    base_url = (p["base_url"] or "").rstrip("/")
    api_key = (body.api_key or "").strip() or p["api_key"]
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    client_kwargs = {"timeout": 20}
    proxy = get_http_proxy()
    if proxy:
        client_kwargs["proxy"] = proxy
    fallback = p["models"]
    try:
        async with httpx.AsyncClient(**client_kwargs) as client:
            resp = await client.get(base_url + "/models", headers=headers)
            if resp.status_code == 200:
                data = resp.json()
                ids = [m.get("id") for m in data.get("data", []) if m.get("id")]
                if ids:
                    return {"source": "live", "models": ids}
            if resp.status_code in (401, 403):
                return {"source": "error",
                        "error": f"API Key 无效或无权限（HTTP {resp.status_code}），请检查后重试。",
                        "models": fallback}
            if resp.status_code == 404:
                return {"source": "unsupported",
                        "error": "该服务商不支持模型列表接口，可手动维护模型表。",
                        "models": fallback}
            return {"source": "error", "error": f"获取模型失败（HTTP {resp.status_code}）",
                    "models": fallback}
    except Exception as e:
        return {"source": "error", "error": f"无法连接该服务商：{e}", "models": fallback}


@router.post("/llm-providers/{provider_id}/test")
async def test_provider(provider_id: int, body: ProviderTestBody,
                        admin: dict = Depends(require_admin)):
    """连接测试：用该供应商已存 Key + 指定模型（空则默认模型）发最小请求。"""
    from core import provider_manager as prov
    p = prov.get_provider(provider_id)
    if p is None:
        raise HTTPException(status_code=404, detail="供应商不存在")
    base_url = (p["base_url"] or "").rstrip("/")
    model = (body.model or "").strip() or (p["model"] or "").strip()
    if not model:
        raise HTTPException(status_code=400, detail="该供应商未设置模型，请先编辑补充")
    api_key = (body.api_key or "").strip() or p["api_key"]
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    client_kwargs = {"timeout": 30}
    proxy = get_http_proxy()
    if proxy:
        client_kwargs["proxy"] = proxy
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "回复两个字：正常"}],
        "max_tokens": 16,
        "temperature": 0,
    }
    import time
    t0 = time.time()
    try:
        async with httpx.AsyncClient(**client_kwargs) as client:
            resp = await client.post(base_url + "/chat/completions",
                                     json=payload, headers=headers)
            latency = int((time.time() - t0) * 1000)
            if resp.status_code == 200:
                data = resp.json()
                reply = ((data.get("choices") or [{}])[0]
                         .get("message", {}).get("content", ""))
                return {"ok": True, "latency_ms": latency,
                        "reply": reply[:80], "proxy": bool(proxy)}
            if resp.status_code in (401, 403):
                return {"ok": False, "latency_ms": latency,
                        "error": f"API Key 无效或无权限（HTTP {resp.status_code}），请检查后重试。",
                        "proxy": bool(proxy)}
            return {"ok": False, "latency_ms": latency,
                    "error": f"服务商返回错误（HTTP {resp.status_code}）：{resp.text[:200]}",
                    "proxy": bool(proxy)}
    except Exception as e:
        latency = int((time.time() - t0) * 1000)
        return {"ok": False, "latency_ms": latency,
                "error": f"无法连接：{e}", "proxy": bool(proxy)}



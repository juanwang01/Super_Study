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


class LLMConfigBody(BaseModel):
    base_url: str | None = None
    api_key: str | None = None
    model: str | None = None
    timeout: str | None = None


class ModelsBody(BaseModel):
    base_url: str
    api_key: str = ""


class LLMTestBody(BaseModel):
    base_url: str
    api_key: str = ""
    model: str = ""


def _mask_key(key: str) -> dict[str, bool | str]:
    if not key:
        return {"configured": False, "masked": ""}
    tail = key[-4:] if len(key) > 4 else key
    return {"configured": True, "masked": f"****{tail}"}


@router.get("/llm-providers")
def get_providers(admin: dict = Depends(require_admin)):
    """返回预置算力服务商列表（不含任何 Key）。"""
    return {"providers": PROVIDERS}


@router.post("/llm-models")
async def fetch_models(body: ModelsBody, admin: dict = Depends(require_admin)):
    """用给定 base_url + api_key 拉取该服务商真实模型列表（调 /models）。

    api_key 为空时使用服务端已保存的 Key。Key 不做任何存储。失败时返回预置模型表或错误信息。
    """
    base_url = (body.base_url or "").rstrip("/")
    if not base_url:
        raise HTTPException(status_code=400, detail="接口地址不能为空")
    api_key = (body.api_key or "").strip() or get_llm_config()["api_key"]

    # 先匹配预置服务商，失败时至少能给出候选模型
    preset = next((p for p in PROVIDERS if p["base_url"].rstrip("/") == base_url), None)
    fallback_models = preset["models"] if preset else []

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
            # 401/403 说明 Key 无效
            if resp.status_code in (401, 403):
                return {"source": "error",
                        "error": "API Key 无效或无权限（HTTP " + str(resp.status_code) + "），请检查后重试。",
                        "models": fallback_models}
            if resp.status_code == 404:
                return {"source": "unsupported",
                        "error": "该服务商不支持模型列表接口，请从预置模型中选择。",
                        "models": fallback_models}
            return {"source": "error",
                    "error": f"获取模型失败（HTTP {resp.status_code}）",
                    "models": fallback_models}
    except Exception as e:
        return {"source": "error",
                "error": f"无法连接该服务商：{e}",
                "models": fallback_models}


@router.post("/llm-test")
async def test_llm(body: LLMTestBody, admin: dict = Depends(require_admin)):
    """连接测试：用当前填写的地址/Key/模型发一个最小请求，返回延迟与结果。

    Key 为空时使用服务端已保存的 Key。只用于本次测试，不做存储。
    """
    base_url = (body.base_url or "").rstrip("/")
    model = (body.model or "").strip()
    if not base_url:
        raise HTTPException(status_code=400, detail="接口地址不能为空")
    if not model:
        raise HTTPException(status_code=400, detail="请先选择模型")
    api_key = (body.api_key or "").strip() or get_llm_config()["api_key"]

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


@router.get("/llm-config")
def get_llm_config_api(admin: dict = Depends(require_admin)):
    cfg = get_llm_config()
    return {
        "base_url": cfg["base_url"],
        "model": cfg["model"],
        "timeout": cfg["timeout"],
        "api_key": _mask_key(cfg["api_key"]),
    }


@router.post("/llm-config")
def set_llm_config_api(body: LLMConfigBody, admin: dict = Depends(require_admin)):
    try:
        cfg = save_llm_config(
            base_url=body.base_url,
            api_key=body.api_key,
            model=body.model,
            timeout=body.timeout,
        )
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"配置文件写入失败：{e}")
    return {
        "saved": True,
        "base_url": cfg["base_url"],
        "model": cfg["model"],
        "timeout": cfg["timeout"],
        "api_key": _mask_key(cfg["api_key"]),
        "note": "配置已保存到服务端 .env 文件，立即生效。",
    }

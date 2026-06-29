"""AI Chat route — прямой чат с LLM провайдерами (byesu/openai, deepseek, groq)."""

from __future__ import annotations

import os
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException
from loguru import logger
from pydantic import BaseModel

router = APIRouter(prefix="/api/chat", tags=["chat"])

LLM_TIMEOUT = 60.0


def _env_bool(key: str, default: bool = False) -> bool:
    v = os.getenv(key)
    if v is None:
        return default
    return v.strip().lower() in {"1", "true", "yes", "on"}


def _env_list(key: str) -> list[str]:
    raw = os.getenv(key, "")
    for delim in [";", "\n", "\r", "\t"]:
        raw = raw.replace(delim, ",")
    return [s.strip() for s in raw.split(",") if s.strip()]


def _unique_keys(keys: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for k in keys:
        if k and k not in seen:
            seen.add(k)
            out.append(k)
    return out


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    provider: str = "openai"
    model: str = ""
    messages: list[ChatMessage] = []
    temperature: float = 0.7
    max_tokens: int = 2048
    system_prompt: str = ""


class ChatResponse(BaseModel):
    ok: bool
    reply: str
    provider: str
    model: str
    error: str = ""


class ProviderInfo(BaseModel):
    name: str
    configured: bool
    base_url: str = ""
    wire_api: str = ""
    model: str = ""
    key_count: int = 0


class TestResponse(BaseModel):
    ok: bool
    provider: str
    model: str
    reply: str = ""
    latency_ms: int = 0
    error: str = ""


def _get_providers() -> list[ProviderInfo]:
    infos: list[ProviderInfo] = []

    openai_keys = _unique_keys([os.getenv("OPENAI_API_KEY", "").strip(), *_env_list("OPENAI_API_KEYS")])
    infos.append(
        ProviderInfo(
            name="openai",
            configured=bool(openai_keys),
            base_url=os.getenv("OPENAI_BASE_URL", "https://api.openai.com"),
            wire_api=os.getenv("OPENAI_WIRE_API", "responses"),
            model=os.getenv("OPENAI_MODEL", "gpt-5.5"),
            key_count=len(openai_keys),
        )
    )

    ds_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
    infos.append(
        ProviderInfo(
            name="deepseek",
            configured=bool(ds_key),
            base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
            wire_api=os.getenv("DEEPSEEK_WIRE_API", "chat"),
            model=os.getenv("DEEPSEEK_MODEL", "deepseek-v4-pro"),
            key_count=1 if ds_key else 0,
        )
    )

    groq_key = os.getenv("GROQ_API_KEY", "").strip()
    infos.append(
        ProviderInfo(
            name="groq",
            configured=bool(groq_key),
            base_url="https://api.groq.com/openai",
            wire_api="chat",
            model=os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile"),
            key_count=1 if groq_key else 0,
        )
    )

    return infos


def _build_client(provider: str) -> dict[str, Any]:
    if provider == "openai":
        keys = _unique_keys([os.getenv("OPENAI_API_KEY", "").strip(), *_env_list("OPENAI_API_KEYS")])
        if not keys:
            raise ValueError("OPENAI_API_KEY не настроен")
        return {
            "keys": keys,
            "base_url": os.getenv("OPENAI_BASE_URL", "https://api.openai.com").rstrip("/"),
            "wire_api": os.getenv("OPENAI_WIRE_API", "responses").strip().lower(),
            "api_prefix": (os.getenv("OPENAI_API_PREFIX", "/v1") or "").strip("/"),
            "reasoning_effort": os.getenv("OPENAI_REASONING_EFFORT", "").strip(),
            "disable_response_storage": _env_bool("OPENAI_DISABLE_RESPONSE_STORAGE", True),
        }
    if provider == "deepseek":
        key = os.getenv("DEEPSEEK_API_KEY", "").strip()
        if not key:
            raise ValueError("DEEPSEEK_API_KEY не настроен")
        return {
            "keys": [key],
            "base_url": os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/"),
            "wire_api": os.getenv("DEEPSEEK_WIRE_API", "chat").strip().lower(),
            "api_prefix": (os.getenv("DEEPSEEK_API_PREFIX", "") or "").strip("/"),
            "reasoning_effort": os.getenv("DEEPSEEK_REASONING_EFFORT", "").strip(),
            "disable_response_storage": True,
        }
    if provider == "groq":
        key = os.getenv("GROQ_API_KEY", "").strip()
        if not key:
            raise ValueError("GROQ_API_KEY не настроен")
        return {
            "keys": [key],
            "base_url": "https://api.groq.com/openai",
            "wire_api": "chat",
            "api_prefix": "/v1",
            "reasoning_effort": "",
            "disable_response_storage": True,
        }
    raise ValueError(f"Неизвестный провайдер: {provider}")


def _default_model(provider: str) -> str:
    if provider == "openai":
        return os.getenv("OPENAI_MODEL", "gpt-5.5")
    if provider == "deepseek":
        return os.getenv("DEEPSEEK_MODEL", "deepseek-v4-pro")
    if provider == "groq":
        return os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
    return ""


def _build_url(cfg: dict[str, Any], endpoint: str) -> str:
    base = cfg["base_url"]
    prefix = cfg["api_prefix"]
    if prefix and not base.rstrip("/").endswith(f"/{prefix}"):
        base = f"{base}/{prefix}"
    return f"{base}/{endpoint.lstrip('/')}"


def _extract_text(data: dict[str, Any]) -> str:
    text = data.get("output_text")
    if isinstance(text, str) and text.strip():
        return text

    choices = data.get("choices") or []
    if choices:
        msg = choices[0].get("message") or {}
        content = msg.get("content")
        if isinstance(content, str) and content.strip():
            return content
        if isinstance(content, list):
            joined = "".join(item.get("text", "") for item in content if isinstance(item, dict))
            if joined.strip():
                return joined

    output = data.get("output") or []
    parts: list[str] = []
    for item in output:
        if not isinstance(item, dict):
            continue
        for c in item.get("content") or []:
            if isinstance(c, dict):
                t = c.get("text")
                if isinstance(t, str):
                    parts.append(t)
    joined = "".join(parts).strip()
    if joined:
        return joined

    raise ValueError("Провайдер вернул пустой ответ")


async def _call_llm(
    cfg: dict[str, Any],
    model: str,
    messages: list[dict[str, str]],
    temperature: float,
    max_tokens: int,
) -> str:
    headers = {
        "Authorization": f"Bearer {cfg['keys'][0]}",
        "Content-Type": "application/json",
    }

    if cfg["wire_api"] == "responses":
        payload: dict[str, Any] = {
            "model": model,
            "input": messages,
            "temperature": temperature,
            "max_output_tokens": max_tokens,
            "store": not cfg["disable_response_storage"],
        }
        if cfg["reasoning_effort"]:
            payload["reasoning"] = {"effort": cfg["reasoning_effort"]}
        url = _build_url(cfg, "responses")
    else:
        payload = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if cfg["reasoning_effort"]:
            payload["reasoning_effort"] = cfg["reasoning_effort"]
        url = _build_url(cfg, "chat/completions")

    async with httpx.AsyncClient(timeout=LLM_TIMEOUT, trust_env=False) as client:
        resp = await client.post(url, headers=headers, json=payload)
        resp.raise_for_status()
        return _extract_text(resp.json())


@router.get("/providers", response_model=list[ProviderInfo])
def get_providers():
    return _get_providers()


@router.post("/test", response_model=TestResponse)
async def test_provider(req: ChatRequest):
    import time

    provider = req.provider
    try:
        cfg = _build_client(provider)
    except ValueError as e:
        return TestResponse(ok=False, provider=provider, model=req.model, error=str(e))

    model = req.model or _default_model(provider)
    messages = [{"role": "user", "content": "Скажи «привет» одним словом."}]

    t0 = time.perf_counter()
    try:
        reply = await _call_llm(cfg, model, messages, temperature=0.3, max_tokens=64)
        latency = int((time.perf_counter() - t0) * 1000)
        logger.info(f"Chat test: provider={provider} model={model} OK latency={latency}ms reply={reply[:50]!r}")
        return TestResponse(ok=True, provider=provider, model=model, reply=reply, latency_ms=latency)
    except Exception as e:
        latency = int((time.perf_counter() - t0) * 1000)
        logger.warning(f"Chat test: provider={provider} model={model} FAIL: {e}")
        return TestResponse(ok=False, provider=provider, model=model, latency_ms=latency, error=str(e))


@router.post("/send", response_model=ChatResponse)
async def send_message(req: ChatRequest):
    provider = req.provider
    try:
        cfg = _build_client(provider)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    model = req.model or _default_model(provider)

    messages: list[dict[str, str]] = []
    if req.system_prompt:
        messages.append({"role": "system", "content": req.system_prompt})
    for m in req.messages:
        messages.append({"role": m.role, "content": m.content})

    if not messages:
        raise HTTPException(status_code=400, detail="Сообщения пусты")

    try:
        reply = await _call_llm(cfg, model, messages, req.temperature, req.max_tokens)
        logger.info(f"Chat send: provider={provider} model={model} msgs={len(messages)} reply_len={len(reply)}")
        return ChatResponse(ok=True, reply=reply, provider=provider, model=model)
    except httpx.HTTPStatusError as e:
        body = ""
        try:
            body = e.response.text[:500]
        except Exception:
            pass
        logger.warning(f"Chat send HTTP error: provider={provider} status={e.response.status_code} body={body}")
        raise HTTPException(status_code=502, detail=f"HTTP {e.response.status_code}: {body or str(e)}")
    except Exception as e:
        logger.warning(f"Chat send error: provider={provider} model={model}: {e}")
        raise HTTPException(status_code=500, detail=str(e))

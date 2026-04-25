"""
Health-aware LLM router.

Поддерживает task-specific выбор модели и fallback между провайдерами.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx
from loguru import logger


class GLMClient:
    """Клиент для GLM API (Zhipu AI)."""

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or os.getenv("GLM_API_KEY")
        self.base_url = "https://open.bigmodel.cn/api/paas/v4"

        if not self.api_key:
            raise ValueError("GLM_API_KEY не задан")

        parts = self.api_key.split(".")
        if len(parts) != 2:
            raise ValueError("GLM ключ должен быть в формате: id.secret")

        self.key_id = parts[0]
        self.key_secret = parts[1]

    def _get_token(self) -> str:
        import jwt

        now = int(time.time())
        payload = {
            "api_key": self.key_id,
            "exp": now + 3600,
            "timestamp": now,
        }
        return jwt.encode(
            payload,
            self.key_secret,
            algorithm="HS256",
            headers={"alg": "HS256", "sign_type": "SIGN"},
        )

    async def generate(
        self,
        prompt: str,
        model: str = "glm-4-air",
        temperature: float = 0.7,
        max_tokens: int = 500,
        system_prompt: Optional[str] = None,
    ) -> str:
        token = self._get_token()
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})
        payload = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(
                f"{self.base_url}/chat/completions",
                headers=headers,
                json=payload,
            )
            response.raise_for_status()
            data = response.json()
            return data["choices"][0]["message"]["content"]


@dataclass
class ProviderHealth:
    success: int = 0
    failure: int = 0
    consecutive_failures: int = 0
    last_success_at: float = 0.0
    last_failure_at: float = 0.0
    last_error: str = ""
    last_model: str = ""
    tasks: dict[str, dict[str, int]] = field(default_factory=dict)

    def score(self) -> float:
        success_bonus = min(self.success, 20) * 2.0
        failure_penalty = min(self.failure, 20) * 3.0
        streak_penalty = self.consecutive_failures * 12.0
        freshness_bonus = 8.0 if self.last_success_at and self.last_success_at >= self.last_failure_at else 0.0
        return 100.0 + success_bonus + freshness_bonus - failure_penalty - streak_penalty

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "failure": self.failure,
            "consecutive_failures": self.consecutive_failures,
            "last_success_at": self.last_success_at,
            "last_failure_at": self.last_failure_at,
            "last_error": self.last_error,
            "last_model": self.last_model,
            "tasks": self.tasks,
            "health_score": round(self.score(), 1),
        }


class LLMRouter:
    """
    Роутер для выбора LLM провайдера.

    Поддерживает:
    - task-specific provider/model routing
    - health-aware fallback
    - in-memory provider diagnostics
    """

    def __init__(self):
        self.providers: dict[str, Any] = {}
        self.health: dict[str, ProviderHealth] = {}
        self._last_route: dict[str, Any] = {}
        self._init_providers()

    def _init_providers(self) -> None:
        groq_key = os.getenv("GROQ_API_KEY")
        if groq_key:
            self.providers["groq"] = {"api_key": groq_key}
            self.health["groq"] = ProviderHealth()
            logger.info("LLMRouter: Groq подключен")

        google_key = os.getenv("GOOGLE_API_KEY")
        if google_key:
            try:
                import google.generativeai as genai

                genai.configure(api_key=google_key)
                self.providers["google"] = genai
                self.health["google"] = ProviderHealth()
                logger.info("LLMRouter: Google подключен")
            except Exception as e:
                logger.warning(f"LLMRouter: Google недоступен: {e}")

        glm_key = os.getenv("GLM_API_KEY")
        if glm_key:
            try:
                self.providers["glm"] = GLMClient(glm_key)
                self.health["glm"] = ProviderHealth()
                logger.info("LLMRouter: GLM подключен")
            except Exception as e:
                logger.warning(f"LLMRouter: GLM недоступен: {e}")

    async def generate(
        self,
        prompt: str,
        provider: Optional[str] = None,
        model: Optional[str] = None,
        temperature: float = 0.75,
        max_tokens: int = 500,
        task: str = "general",
        system_prompt: Optional[str] = None,
    ) -> str:
        candidates = self._candidate_providers(task=task, preferred=provider)
        if not candidates:
            raise ValueError("Нет доступных LLM провайдеров")

        last_error = None
        for provider_name in candidates:
            selected_model = self._select_model(provider_name, task, model)
            try:
                result = await self._generate_once(
                    provider=provider_name,
                    prompt=prompt,
                    model=selected_model,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    system_prompt=system_prompt,
                )
                self._record_success(provider_name, task, selected_model)
                self._last_route = {
                    "provider": provider_name,
                    "model": selected_model,
                    "task": task,
                    "at": time.time(),
                }
                logger.debug(f"LLMRouter: task={task} provider={provider_name} model={selected_model}")
                return result
            except Exception as e:
                self._record_failure(provider_name, task, selected_model, e)
                last_error = e
                logger.warning(
                    f"LLMRouter: provider={provider_name} task={task} failed, try next: {e}"
                )

        raise ValueError(f"Все LLM провайдеры недоступны для task={task}: {last_error}")

    async def _generate_once(
        self,
        provider: str,
        prompt: str,
        model: str,
        temperature: float,
        max_tokens: int,
        system_prompt: Optional[str],
    ) -> str:
        if provider == "groq":
            return await self._generate_groq(prompt, model, temperature, max_tokens, system_prompt)
        if provider == "google":
            return await self._generate_google(prompt, model, temperature, max_tokens, system_prompt)
        if provider == "glm":
            return await self._generate_glm(prompt, model, temperature, max_tokens, system_prompt)
        raise ValueError(f"Неизвестный провайдер: {provider}")

    def _candidate_providers(self, task: str, preferred: Optional[str]) -> list[str]:
        preferred_provider = self._normalize_provider(preferred)
        if preferred_provider and preferred_provider in self.providers:
            ordered = [preferred_provider]
        else:
            env_preferred = self._task_preferred_provider(task)
            ordered = [env_preferred] if env_preferred in self.providers else []

        ranked = sorted(
            self.providers.keys(),
            key=lambda name: self.health.get(name, ProviderHealth()).score(),
            reverse=True,
        )

        result: list[str] = []
        for name in ordered + ranked:
            if name in self.providers and name not in result:
                result.append(name)
        return result

    def _task_preferred_provider(self, task: str) -> Optional[str]:
        env_key = f"{task.upper()}_PROVIDER".replace("-", "_")
        value = os.getenv(env_key)
        return self._normalize_provider(value)

    def _select_model(self, provider: str, task: str, explicit_model: Optional[str]) -> str:
        if explicit_model:
            return explicit_model

        task_key = task.upper().replace("-", "_")
        provider_key = provider.upper()

        candidates = [
            f"{provider_key}_MODEL_{task_key}",
            f"{task_key}_MODEL_{provider_key}",
            f"{provider_key}_MODEL",
            f"{task_key}_MODEL",
        ]
        for env_key in candidates:
            value = os.getenv(env_key)
            if value:
                return value

        defaults = {
            "groq": {
                "query_generation": "llama-3.1-8b-instant",
                "scoring": "llama-3.3-70b-versatile",
                "proposal_writing": "llama-3.3-70b-versatile",
                "general": "llama-3.3-70b-versatile",
            },
            "google": {
                "query_generation": "gemini-2.5-flash",
                "scoring": "gemini-2.5-flash",
                "proposal_writing": "gemini-2.5-pro",
                "general": "gemini-2.5-flash",
            },
            "glm": {
                "query_generation": "glm-4-air",
                "scoring": "glm-4-air",
                "proposal_writing": "glm-4-air",
                "general": "glm-4-air",
            },
        }
        return defaults.get(provider, {}).get(task, defaults.get(provider, {}).get("general", ""))

    def _normalize_provider(self, provider: Optional[str]) -> Optional[str]:
        if not provider:
            return None
        provider = provider.strip().lower()
        if provider == "auto":
            return None
        return provider

    def _record_success(self, provider: str, task: str, model: str) -> None:
        health = self.health.setdefault(provider, ProviderHealth())
        health.success += 1
        health.consecutive_failures = 0
        health.last_success_at = time.time()
        health.last_model = model
        bucket = health.tasks.setdefault(task, {"success": 0, "failure": 0})
        bucket["success"] += 1

    def _record_failure(self, provider: str, task: str, model: str, error: Exception) -> None:
        health = self.health.setdefault(provider, ProviderHealth())
        health.failure += 1
        health.consecutive_failures += 1
        health.last_failure_at = time.time()
        health.last_error = str(error)
        health.last_model = model
        bucket = health.tasks.setdefault(task, {"success": 0, "failure": 0})
        bucket["failure"] += 1

    async def _generate_groq(
        self,
        prompt: str,
        model: str,
        temperature: float,
        max_tokens: int,
        system_prompt: Optional[str],
    ) -> str:
        from groq import AsyncGroq

        client = AsyncGroq(api_key=self.providers["groq"]["api_key"])
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})
        response = await client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        content = response.choices[0].message.content
        if not content:
            raise ValueError("Groq вернул пустой ответ")
        return content

    async def _generate_google(
        self,
        prompt: str,
        model: str,
        temperature: float,
        max_tokens: int,
        system_prompt: Optional[str],
    ) -> str:
        genai = self.providers["google"]
        gen_model = (
            genai.GenerativeModel(model, system_instruction=system_prompt)
            if system_prompt
            else genai.GenerativeModel(model)
        )
        response = await gen_model.generate_content_async(
            prompt,
            generation_config={
                "temperature": temperature,
                "max_output_tokens": max_tokens,
            },
        )

        text = getattr(response, "text", None)
        if text:
            return text

        candidates = getattr(response, "candidates", None) or []
        if candidates:
            parts = getattr(candidates[0], "content", None)
            if parts and getattr(parts, "parts", None):
                joined = "".join(part.text for part in parts.parts if getattr(part, "text", None))
                if joined.strip():
                    return joined
        raise ValueError("Google вернул пустой ответ")

    async def _generate_glm(
        self,
        prompt: str,
        model: str,
        temperature: float,
        max_tokens: int,
        system_prompt: Optional[str],
    ) -> str:
        client = self.providers["glm"]
        return await client.generate(
            prompt=prompt,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            system_prompt=system_prompt,
        )

    def get_provider_health(self) -> dict[str, dict[str, Any]]:
        return {
            name: health.to_dict()
            for name, health in self.health.items()
        }

    def get_last_route(self) -> dict[str, Any]:
        return dict(self._last_route)


llm_router: LLMRouter | None = None


def get_llm_router() -> LLMRouter:
    global llm_router
    if llm_router is None:
        llm_router = LLMRouter()
    return llm_router

"""
Health-aware LLM router.

Поддерживает task-specific выбор модели и fallback между провайдерами.

Маршрутизация:
- Task-specific provider: env {TASK}_PROVIDER (e.g. QUERY_GENERATION_PROVIDER=groq)
- Task-specific model: env {PROVIDER}_MODEL_{TASK} (e.g. GROQ_MODEL_SCORING=llama-3.3-70b-versatile)
- Fallback: по убыванию health-score среди зарегистрированных провайдеров
- Новые провайдеры подключаются через API-ключ в .env без изменения кода вызывающих модулей

Requirements: 10.1, 10.2, 10.3, 10.4, 10.5
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx
from loguru import logger

# Таймаут для LLM-запросов (секунды). Requirement 10.2: таймаут >60с = ошибка.
LLM_REQUEST_TIMEOUT = 60.0


def _env_bool(key: str, default: bool = False) -> bool:
    value = os.getenv(key)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_list(key: str) -> list[str]:
    value = os.getenv(key, "")
    for delimiter in [";", "\n", "\r", "\t"]:
        value = value.replace(delimiter, ",")
    return [item.strip() for item in value.split(",") if item.strip()]


def _unique_keys(keys: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for key in keys:
        if not key:
            continue
        if key not in seen:
            seen.add(key)
            result.append(key)
    return result


class OpenAICompatibleClient:
    """Minimal async client for OpenAI-compatible chat and responses APIs."""

    def __init__(
        self,
        *,
        api_key: str = "",
        api_keys: Optional[list[str]] = None,
        base_url: str,
        wire_api: str = "chat",
        api_prefix: str = "/v1",
        disable_response_storage: bool = True,
        reasoning_effort: str = "",
        thinking: str = "",
    ):
        self.api_keys = _unique_keys([*(api_keys or []), api_key] if api_key else (api_keys or []))
        if not self.api_keys:
            raise ValueError("API key is not set")
        self._api_key_index = 0
        self.base_url = base_url.rstrip("/")
        self.wire_api = (wire_api or "chat").strip().lower()
        self.api_prefix = (api_prefix or "").strip("/")
        self.disable_response_storage = disable_response_storage
        self.reasoning_effort = reasoning_effort.strip()
        self.thinking = thinking.strip().lower()

    def _url(self, endpoint: str) -> str:
        base = self.base_url
        if self.api_prefix and not base.rstrip("/").endswith(f"/{self.api_prefix}"):
            base = f"{base}/{self.api_prefix}"
        return f"{base}/{endpoint.lstrip('/')}"

    async def generate(
        self,
        prompt: str,
        model: str,
        temperature: float,
        max_tokens: int,
        system_prompt: Optional[str] = None,
    ) -> str:
        if self.wire_api == "responses":
            return await self._generate_responses(prompt, model, temperature, max_tokens, system_prompt)
        return await self._generate_chat(prompt, model, temperature, max_tokens, system_prompt)

    def _headers(self) -> dict[str, str]:
        api_key = self._next_api_key()
        return {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

    def _next_api_key(self) -> str:
        api_key = self.api_keys[self._api_key_index % len(self.api_keys)]
        self._api_key_index = (self._api_key_index + 1) % len(self.api_keys)
        return api_key

    def _messages(self, prompt: str, system_prompt: Optional[str]) -> list[dict[str, str]]:
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})
        return messages

    async def _generate_chat(
        self,
        prompt: str,
        model: str,
        temperature: float,
        max_tokens: int,
        system_prompt: Optional[str],
    ) -> str:
        payload: dict[str, Any] = {
            "model": model,
            "messages": self._messages(prompt, system_prompt),
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if self.reasoning_effort and self.thinking != "disabled":
            payload["reasoning_effort"] = self.reasoning_effort
        if self.thinking in {"enabled", "disabled"}:
            payload["thinking"] = {"type": self.thinking}

        async with httpx.AsyncClient(timeout=LLM_REQUEST_TIMEOUT, trust_env=False) as client:
            response = await client.post(self._url("chat/completions"), headers=self._headers(), json=payload)
            response.raise_for_status()
            return self._extract_text(response.json())

    async def _generate_responses(
        self,
        prompt: str,
        model: str,
        temperature: float,
        max_tokens: int,
        system_prompt: Optional[str],
    ) -> str:
        payload: dict[str, Any] = {
            "model": model,
            "input": self._messages(prompt, system_prompt),
            "temperature": temperature,
            "max_output_tokens": max_tokens,
            "store": not self.disable_response_storage,
        }
        if self.reasoning_effort:
            payload["reasoning"] = {"effort": self.reasoning_effort}

        async with httpx.AsyncClient(timeout=LLM_REQUEST_TIMEOUT, trust_env=False) as client:
            response = await client.post(self._url("responses"), headers=self._headers(), json=payload)
            response.raise_for_status()
            return self._extract_text(response.json())

    @staticmethod
    def _extract_text(data: dict[str, Any]) -> str:
        output_text = data.get("output_text")
        if isinstance(output_text, str) and output_text.strip():
            return output_text

        choices = data.get("choices") or []
        if choices:
            message = choices[0].get("message") or {}
            content = message.get("content")
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
            for content in item.get("content") or []:
                if not isinstance(content, dict):
                    continue
                text = content.get("text")
                if isinstance(text, str):
                    parts.append(text)
        joined = "".join(parts).strip()
        if joined:
            return joined

        raise ValueError("OpenAI-compatible provider returned empty response")


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
        """
        Автоматическое обнаружение провайдеров по API-ключам в окружении.

        Requirement 10.5: Новые провайдеры добавляются через API-ключ в .env
        и становятся доступными для маршрутизации без изменения кода.

        Поддерживаемые провайдеры:
        - GROQ_API_KEY → groq
        """
        openai_keys = _unique_keys([os.getenv("OPENAI_API_KEY", "").strip(), *_env_list("OPENAI_API_KEYS")])
        if openai_keys:
            try:
                self.providers["openai"] = OpenAICompatibleClient(
                    api_keys=openai_keys,
                    base_url=os.getenv("OPENAI_BASE_URL", "https://api.openai.com"),
                    wire_api=os.getenv("OPENAI_WIRE_API", "responses"),
                    api_prefix=os.getenv("OPENAI_API_PREFIX", "/v1"),
                    disable_response_storage=_env_bool("OPENAI_DISABLE_RESPONSE_STORAGE", True),
                    reasoning_effort=os.getenv("OPENAI_REASONING_EFFORT", os.getenv("MODEL_REASONING_EFFORT", "")),
                )
                self.health["openai"] = ProviderHealth()
                logger.info(f"LLMRouter: OpenAI-compatible provider подключен (keys={len(openai_keys)})")
            except Exception as e:
                logger.warning(f"LLMRouter: OpenAI-compatible provider недоступен: {e}")

        deepseek_key = os.getenv("DEEPSEEK_API_KEY")
        if deepseek_key:
            try:
                self.providers["deepseek"] = OpenAICompatibleClient(
                    api_key=deepseek_key,
                    base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
                    wire_api=os.getenv("DEEPSEEK_WIRE_API", "chat"),
                    api_prefix=os.getenv("DEEPSEEK_API_PREFIX", ""),
                    disable_response_storage=True,
                    reasoning_effort=os.getenv("DEEPSEEK_REASONING_EFFORT", ""),
                    thinking=os.getenv("DEEPSEEK_THINKING", ""),
                )
                self.health["deepseek"] = ProviderHealth()
                logger.info("LLMRouter: DeepSeek подключен")
            except Exception as e:
                logger.warning(f"LLMRouter: DeepSeek недоступен: {e}")

        groq_key = os.getenv("GROQ_API_KEY")
        if groq_key:
            self.providers["groq"] = {"api_key": groq_key}
            self.health["groq"] = ProviderHealth()
            logger.info("LLMRouter: Groq подключен")

        if not self.providers:
            logger.warning("LLMRouter: Ни один провайдер не подключен. Добавьте API-ключ в .env")

    async def generate(
        self,
        prompt: str,
        provider: Optional[str] = None,
        model: Optional[str] = None,
        temperature: float = 0.75,
        max_tokens: int = 2048,
        task: str = "general",
        system_prompt: Optional[str] = None,
    ) -> str:
        candidates = self._candidate_providers(task=task, preferred=provider)
        if not candidates:
            raise ValueError("Нет доступных LLM провайдеров")

        preferred_provider = self._normalize_provider(provider)
        last_error = None
        for provider_name in candidates:
            model_for_provider = model if not preferred_provider or provider_name == preferred_provider else None
            selected_model = self._select_model(provider_name, task, model_for_provider)
            logger.info(f"LLMRouter: task={task} provider={provider_name} model={selected_model} start")
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
                logger.info(f"LLMRouter: task={task} provider={provider_name} model={selected_model} success")
                return result
            except Exception as e:
                self._record_failure(provider_name, task, selected_model, e)
                last_error = e
                logger.warning(
                    f"LLMRouter: task={task} provider={provider_name} model={selected_model} failed, try next: {e}"
                )

        # Requirement 10.3: если все провайдеры вернули ошибку — исключение с task и ошибкой последнего
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
        if provider in {"openai", "deepseek"}:
            return await self._generate_openai_compatible(
                provider, prompt, model, temperature, max_tokens, system_prompt
            )
        if provider == "groq":
            return await self._generate_groq(prompt, model, temperature, max_tokens, system_prompt)
        raise ValueError(f"Неизвестный провайдер: {provider}")

    def _candidate_providers(self, task: str, preferred: Optional[str]) -> list[str]:
        """
        Определяет порядок провайдеров для попытки генерации.

        Requirement 10.1: task-specific preferred provider через env {TASK}_PROVIDER.
        Requirement 10.2: fallback по убыванию health-score.

        Порядок: preferred (explicit или из env) → остальные по health-score desc.
        """
        preferred_provider = self._normalize_provider(preferred)
        if preferred_provider and preferred_provider in self.providers:
            ordered = [preferred_provider]
        else:
            env_preferred = self._task_preferred_provider(task)
            ordered = [env_preferred] if env_preferred and env_preferred in self.providers else []

        # Requirement 10.2: сортировка по убыванию health-score
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
        """
        Requirement 10.1: Читает {TASK}_PROVIDER из env.
        Например: QUERY_GENERATION_PROVIDER=deepseek, SCORING_PROVIDER=openai.
        """
        env_key = f"{task.upper()}_PROVIDER".replace("-", "_")
        value = os.getenv(env_key)
        provider = self._normalize_provider(value)
        if provider:
            return provider
        if task in {"query_generation", "scoring"}:
            provider = self._normalize_provider(os.getenv("PARSER_LLM_PROVIDER"))
            if provider:
                return provider
        return self._normalize_provider(os.getenv("LLM_PROVIDER"))

    def _select_model(self, provider: str, task: str, explicit_model: Optional[str]) -> str:
        """
        Requirement 10.1: Выбор модели для провайдера и задачи.

        Приоритет:
        1. Явно переданная модель (explicit_model)
        2. Env {PROVIDER}_MODEL_{TASK} (e.g. GROQ_MODEL_SCORING)
        3. Env {TASK}_MODEL_{PROVIDER}
        4. Env {PROVIDER}_MODEL
        5. Env {TASK}_MODEL
        6. Встроенные defaults
        """
        if explicit_model:
            return self._normalize_model(provider, explicit_model)

        task_key = task.upper().replace("-", "_")
        provider_key = provider.upper()

        candidates = [
            f"{provider_key}_MODEL_{task_key}",
            f"{task_key}_MODEL_{provider_key}",
            "PARSER_LLM_MODEL" if self._should_use_parser_model(provider, task) else "",
            f"{provider_key}_MODEL",
            f"{task_key}_MODEL",
        ]
        for env_key in candidates:
            if not env_key:
                continue
            value = os.getenv(env_key)
            if value:
                if env_key == "PARSER_LLM_MODEL" and not self._is_model_compatible(provider, value):
                    logger.warning(f"LLMRouter: skip incompatible PARSER_LLM_MODEL={value!r} for provider={provider}")
                    continue
                return self._normalize_model(provider, value)

        defaults = {
            "openai": {
                "query_generation": "gpt-5.5",
                "scoring": "gpt-5.5",
                "proposal_writing": "gpt-5.5",
                "general": "gpt-5.5",
            },
            "deepseek": {
                "query_generation": "deepseek-v4-pro",
                "scoring": "deepseek-v4-pro",
                "proposal_writing": "deepseek-v4-pro",
                "general": "deepseek-v4-pro",
            },
            "groq": {
                "query_generation": "llama-3.1-8b-instant",
                "scoring": "llama-3.3-70b-versatile",
                "proposal_writing": "llama-3.3-70b-versatile",
                "general": "llama-3.3-70b-versatile",
            },
        }
        selected = defaults.get(provider, {}).get(task, defaults.get(provider, {}).get("general", ""))
        return self._normalize_model(provider, selected)

    def _normalize_provider(self, provider: Optional[str]) -> Optional[str]:
        if not provider:
            return None
        provider = provider.strip().lower()
        if provider == "auto":
            return None
        aliases = {
            "byesu": "openai",
            "gpt": "openai",
            "openai-compatible": "openai",
            "deep seek": "deepseek",
            "deepseek-v4": "deepseek",
            "deepseek_v4": "deepseek",
        }
        provider = aliases.get(provider, provider)
        return provider

    def _should_use_parser_model(self, provider: str, task: str) -> bool:
        if task not in {"query_generation", "scoring"}:
            return False
        parser_provider = self._normalize_provider(os.getenv("PARSER_LLM_PROVIDER"))
        return bool(parser_provider and parser_provider == provider)

    def _is_model_compatible(self, provider: str, model: str) -> bool:
        normalized = self._normalize_model(provider, model).lower().strip()
        if provider == "deepseek":
            return normalized.startswith("deepseek-")
        if provider == "openai":
            return not normalized.startswith(("deepseek-", "llama-", "mixtral-", "gemma-"))
        if provider == "groq":
            return not normalized.startswith(("deepseek-", "gpt-"))
        return True

    @staticmethod
    def _normalize_model(provider: str, model: str) -> str:
        normalized = model.strip()
        if provider == "deepseek":
            deepseek_alias = normalized.lower().replace("_", "-")
            if deepseek_alias in {"v4", "v4-pro", "v4 pro", "pro", "deepseek-pro", "deepseek-v4-pro"}:
                return "deepseek-v4-pro"
            if deepseek_alias in {"flash", "v4-flash", "v4 flash", "deepseek-flash", "deepseek-v4-flash"}:
                return "deepseek-v4-flash"
        return normalized

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

    async def _generate_openai_compatible(
        self,
        provider: str,
        prompt: str,
        model: str,
        temperature: float,
        max_tokens: int,
        system_prompt: Optional[str],
    ) -> str:
        client = self.providers[provider]
        return await client.generate(
            prompt=prompt,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            system_prompt=system_prompt,
        )

    async def _generate_groq(
        self,
        prompt: str,
        model: str,
        temperature: float,
        max_tokens: int,
        system_prompt: Optional[str],
    ) -> str:
        from groq import AsyncGroq

        client = AsyncGroq(
            api_key=self.providers["groq"]["api_key"],
            timeout=LLM_REQUEST_TIMEOUT,
        )
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

    def get_provider_health(self) -> dict[str, dict[str, Any]]:
        """
        Requirement 10.4: Возвращает полную in-memory статистику каждого провайдера.

        Для каждого провайдера возвращает:
        - success: количество успешных вызовов
        - failure: количество ошибок
        - consecutive_failures: число последовательных ошибок
        - last_success_at: timestamp последнего успеха
        - last_failure_at: timestamp последней ошибки
        - last_error: текст последней ошибки
        - last_model: последняя использованная модель
        - tasks: статистика по задачам {task: {success, failure}}
        - health_score: вычисленный health-score
        """
        return {name: health.to_dict() for name, health in self.health.items()}

    def get_last_route(self) -> dict[str, Any]:
        return dict(self._last_route)


llm_router: LLMRouter | None = None


def get_llm_router() -> LLMRouter:
    global llm_router
    if llm_router is None:
        llm_router = LLMRouter()
    return llm_router

"""
GLM (Zhipu AI) LLM Provider
Модели: glm-4v-flash, glm-4.5-air
"""

import os
import requests
from typing import Optional
from loguru import logger


class GLMClient:
    """Клиент для GLM API (Zhipu AI)"""

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or os.getenv("GLM_API_KEY")
        self.base_url = "https://open.bigmodel.cn/api/paas/v4"

        if not self.api_key:
            raise ValueError("GLM_API_KEY не задан")

        # Разделяем ключ на ID и секрет
        parts = self.api_key.split(".")
        if len(parts) != 2:
            raise ValueError("GLM ключ должен быть в формате: id.secret")

        self.key_id = parts[0]
        self.key_secret = parts[1]

    def _get_token(self) -> str:
        """Генерируем JWT токен для авторизации"""
        import time
        import jwt

        payload = {
            "api_key": self.key_id,
            "exp": int(time.time()) + 3600,
            "timestamp": int(time.time()),
        }

        token = jwt.encode(payload, self.key_secret, algorithm="HS256", headers={"alg": "HS256", "sign_type": "SIGN"})
        return token

    async def generate(
        self,
        prompt: str,
        model: str = "glm-4v-flash",
        temperature: float = 0.75,
        max_tokens: int = 500,
    ) -> str:
        """Генерация текста через GLM"""

        token = self._get_token()

        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

        payload = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

        try:
            response = requests.post(
                f"{self.base_url}/chat/completions",
                headers=headers,
                json=payload,
                timeout=60,
            )
            response.raise_for_status()
            data = response.json()

            return data["choices"][0]["message"]["content"]
        except Exception as e:
            logger.error(f"GLM API error: {e}")
            raise


class LLMRouter:
    """
    Роутер для выбора LLM провайдера.
    Поддерживает: groq, google, glm
    """

    def __init__(self):
        self.providers = {}
        self._init_providers()

    def _init_providers(self):
        """Инициализация доступных провайдеров"""
        # Groq
        try:
            from groq import Groq

            groq_key = os.getenv("GROQ_API_KEY")
            if groq_key:
                self.providers["groq"] = Groq(api_key=groq_key)
                logger.info("✅ Groq подключен")
        except Exception as e:
            logger.warning(f"⚠️ Groq не доступен: {e}")

        # Google/Gemini
        try:
            import google.generativeai as genai

            google_key = os.getenv("GOOGLE_API_KEY")
            if google_key:
                genai.configure(api_key=google_key)
                self.providers["google"] = genai
                logger.info("✅ Google подключен")
        except Exception as e:
            logger.warning(f"⚠️ Google не доступен: {e}")

        # GLM
        try:
            self.providers["glm"] = GLMClient()
            logger.info("✅ GLM подключен")
        except Exception as e:
            logger.warning(f"⚠️ GLM не доступен: {e}")

    async def generate(
        self,
        prompt: str,
        provider: Optional[str] = None,
        model: Optional[str] = None,
        temperature: float = 0.75,
        max_tokens: int = 500,
    ) -> str:
        """
        Генерация с автоматическим fallback.

        provider: "groq", "google", "glm" или None (авто)
        model: конкретная модель
        """

        # Если провайдер не указан - выбираем случайно для балансировки
        if not provider:
            import random

            available = list(self.providers.keys())
            if not available:
                raise ValueError("Нет доступных LLM провайдеров")
            provider = random.choice(available)

        # Пробуем указанный провайдер
        try:
            if provider == "groq":
                return await self._generate_groq(prompt, model, temperature, max_tokens)
            elif provider == "google":
                return await self._generate_google(prompt, model, temperature, max_tokens)
            elif provider == "glm":
                return await self._generate_glm(prompt, model, temperature, max_tokens)
        except Exception as e:
            logger.warning(f"{provider} failed: {e}, пробуем fallback...")

            # Fallback на другие провайдеры
            for fallback_provider in ["groq", "google", "glm"]:
                if fallback_provider != provider and fallback_provider in self.providers:
                    try:
                        if fallback_provider == "groq":
                            return await self._generate_groq(prompt, model, temperature, max_tokens)
                        elif fallback_provider == "google":
                            return await self._generate_google(prompt, model, temperature, max_tokens)
                        elif fallback_provider == "glm":
                            return await self._generate_glm(prompt, model, temperature, max_tokens)
                    except Exception as e2:
                        logger.warning(f"Fallback {fallback_provider} failed: {e2}")
                        continue

        raise ValueError("Все LLM провайдеры недоступны")

    async def _generate_groq(self, prompt, model, temperature, max_tokens):
        from groq import AsyncGroq

        client = AsyncGroq(api_key=os.getenv("GROQ_API_KEY"))
        response = await client.chat.completions.create(
            model=model or "llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": prompt}],
            temperature=temperature,
            max_tokens=max_tokens,
        )
        return response.choices[0].message.content

    async def _generate_google(self, prompt, model, temperature, max_tokens):
        model_name = model or os.getenv("GOOGLE_MODEL", "gemini-2.5-flash")
        gen_model = self.providers["google"].GenerativeModel(model_name)
        response = await gen_model.generate_content_async(
            prompt,
            generation_config={
                "temperature": temperature,
                "max_output_tokens": max_tokens,
            },
            safety_settings=[
                {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
                {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
                {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
                {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
            ],
        )
        return response.text

    async def _generate_glm(self, prompt, model, temperature, max_tokens):
        client = self.providers["glm"]
        return await client.generate(
            prompt=prompt,
            model=model or "glm-4.6v",
            temperature=temperature,
            max_tokens=max_tokens,
        )


# Глобальный роутер
llm_router = None


def get_llm_router():
    global llm_router
    if llm_router is None:
        llm_router = LLMRouter()
    return llm_router

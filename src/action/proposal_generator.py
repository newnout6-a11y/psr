"""
Генерация сопроводительных писем через LLM Router (Groq / Gemini / GLM).
"""

import os
import json
import re
from loguru import logger
from typing import Dict, Any, Optional
from .proposal_templates import generate_proposal as generate_template_proposal


class ProposalGenerator:
    """Генератор откликов с использованием LLM."""

    def __init__(self, rag_pipeline=None):
        self.rag = rag_pipeline

        # Загружаем портфолио
        self.portfolio = self._load_portfolio()

        # Системный промпт для РУССКОГО (контроль перплексии, живости текста)
        self.system_prompt_ru = """
Ты — профессиональный разработчик на фрилансе. Напиши короткий и четкий отклик заказчику.
ПРАВИЛА (СТРОГО!):
1. ПРИВЕТСТВИЕ: Всегда начинай со слова "Здравствуйте!". Это единственно допустимое приветствие.
2. НИКАКИХ СМАЙЛИКОВ И ЭМОДЗИ. Текст должен быть профессиональным и чистым.
3. НИКАКОГО MARKDOWN И СПЕЦСИМВОЛОВ. Запрещено: *, #, _, -, списки, жирный шрифт. Только буквы и знаки препинания.
4. СТИЛЬ: Пиши как человек в мессенджере, но вежливо. Сразу к делу.
5. НИКАКОГО ВСТУПИТЕЛЬНОГО ТЕКСТА. Не пиши "Вот вариант", "Конечно". Сразу текст сообщения.
6. РЕЛЕВАНТНЫЙ ОПЫТ: Из своих навыков выбери один пример, который подходит под задачу. Расскажи о нем одним предложением.
7. ВОПРОС: Задай один технический вопрос по задаче заказчика.
8. ДЛИНА: 2-4 предложения. Лаконично.
"""

        # Системный промпт для АНГЛИЙСКОГО
        self.system_prompt_en = """
You are a freelancer developer. Write a proposal for an IT project.
RULES (STRICT!):
1. NO MARKDOWN. Do not use *, #, _, -, or lists. Only plain text.
2. NO ASSISTANT FLUFF. Do not say "Sure!", "Here is your proposal". Start directly with the message.
3. BE SHORT AND REAL. 3-5 sentences maximum.
4. ASK A QUESTION. Use one smart technical question to open the chat.
"""

    def _load_portfolio(self) -> Dict[str, Any]:
        """Загрузка портфолио для контекста."""
        path = "data/portfolio.json"
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        return {"developer": {"experience_years": 5}, "cases": []}

    def _clean_llm_response(self, text: str) -> str:
        """Финальная очистка текста от артефактов ИИ."""
        if not text:
            return ""

        # Удаляем "ассистентские" вступления
        meta = [r"Вот ваш отклик.*", r"Конечно.*", r"Безусловно.*", r"Предлагаю.*", r"Здравствуйте! Вот.*"]
        for p in meta:
            text = re.sub(p, "", text, flags=re.IGNORECASE | re.DOTALL)

        # Удаляем Markdown
        for char in "*#_`>":
            text = text.replace(char, "")

        # Удаляем эмодзи (диапазоны unicode)
        text = re.sub(r"[^\x00-\x7F\u0400-\u04FF\s.,!?()\-:;]", "", text)

        # Удаляем любые упоминания цены/денег (предохранитель)
        price_patterns = [
            r"\d+[\s\d]*\s*(руб|рублей|тыс|тысяч|т\.р|р\.)",
            r"\b(цена|стоимость|бюджет|стоит)\b.*?\d+\s*(руб|р)?",
            r"(бюджет|стоимость):\s*\d+.*",
        ]
        for p in price_patterns:
            text = re.sub(p, "", text, flags=re.IGNORECASE)

        # Удаляем двойные пробелы и переносы
        text = re.sub(r"\s+", " ", text)
        return text.strip()

    def _sanitize_text(self, text: str) -> str:
        """Очистка от анти-спам ловушек и трекеров (honeypots)."""
        if not text:
            return ""
        text = re.sub(r"[A-Za-z0-9+/]{15,}={0,2}", "", text)
        text = re.sub(r"\b[A-Z]{6,}\b", "", text)
        text = re.sub(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b", "", text)
        return re.sub(r"\s+", " ", text).strip()

    def _build_user_prompt(self, project: Any, lang: str = "ru") -> str:
        """Сборка контекста заказа с полным профилем разработчика."""
        proj_skills = ", ".join(project.skills) if project.skills else "указанные в описании"
        budget = f"{project.budget} {project.currency}" if project.budget else "обсуждается"

        dev = self.portfolio.get("developer", {})
        my_skills = ", ".join(dev.get("skills", []))
        exp_years = dev.get("experience_years", 4)

        rag_context = ""
        if self.rag:
            try:
                project_dict = {
                    "title": project.title,
                    "description": project.description,
                    "skills": project.skills,
                }
                rag_context = self.rag.get_portfolio_context(project_dict)
            except Exception as e:
                logger.debug(f"RAG context fallback: {e}")

        if not rag_context:
            highlights = self.portfolio.get("portfolio_highlights", [])
            highlights_text = "; ".join(highlights[:3]) if highlights else "парсинг, автоматизация, веб-разработка"
            rag_context = f"Стек: {my_skills}. Опыт: {exp_years} лет. Примеры: {highlights_text}"

        title_safe = self._sanitize_text(project.title)
        desc_safe = self._sanitize_text(project.description)

        if lang == "en":
            return f"PROJECT: {title_safe}\nDESC: {desc_safe}\nMY PROFILE: {rag_context}\nWrite 3-4 sentences proposal."
        else:
            return f"ЗАКАЗ: {title_safe}\nОПИСАНИЕ: {desc_safe}\nМОЙ ПРОФИЛЬ: {rag_context}\nНапиши ЖИВОЙ отклик заказчику (3-4 предложения) с техническим вопросом."

    async def generate(self, project: Any, provider: Optional[str] = None) -> str:
        """Основной метод генерации с использованием LLM Router (Groq/Google/GLM)."""
        from src.brain.llm_router import get_llm_router

        lang = "en" if project.platform in ["remoteok", "freelancer_com", "peopleperhour", "upwork"] else "ru"

        system_prompt = self.system_prompt_en if lang == "en" else self.system_prompt_ru
        prompt = self._build_user_prompt(project, lang)

        with open("data/last_llm_prompt.txt", "w", encoding="utf-8") as f:
            f.write(prompt)

        # Получаем роутер с поддержкой всех LLM
        router = get_llm_router()

        try:
            # Если провайдер не передан - берем из настроек
            if not provider:
                provider = os.getenv("LLM_PROVIDER", "auto").lower()

            logger.info(f"Генерация отклика через провайдер: {provider}")

            # Выбираем модель в зависимости от провайдера
            if provider == "groq":
                model = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
            elif provider == "google":
                model = os.getenv("GOOGLE_MODEL", "gemini-2.5-flash")
            elif provider == "glm":
                model = os.getenv("GLM_MODEL", "glm-4.6v")
            else:
                model = None  # auto

            # Генерируем через роутер (с автоматическим fallback)
            res = await router.generate(
                prompt=f"{system_prompt}\n\n{prompt}",
                provider=provider if provider != "auto" else None,
                model=model,
                temperature=0.75,
                max_tokens=500,
            )

            if res:
                res = self._clean_llm_response(res)
                with open("data/last_llm_response.txt", "w", encoding="utf-8") as f:
                    f.write(res)
                return res

        except Exception as e:
            logger.error(f"LLM Router Error ({provider}): {e}")

        # Fallback на шаблоны
        logger.warning(f"Все LLM недоступны, используем шаблоны для отклика на {project.title}")
        return generate_template_proposal(
            {"title": project.title, "found_skills": project.skills, "budget": project.budget}, self.portfolio
        )


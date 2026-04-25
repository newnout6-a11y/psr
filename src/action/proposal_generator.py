"""
Генерация сопроводительных писем через LLM Router (Groq / Gemini / GLM).
"""

import os
import json
import re
from loguru import logger
from typing import Dict, Any, Optional
from .proposal_templates import generate_proposal as generate_template_proposal
from src.paths import CASES_FILE, LAST_LLM_PROMPT_FILE, LAST_LLM_RESPONSE_FILE, PORTFOLIO_FILE, ensure_parent


class ProposalGenerator:
    """Генератор откликов с использованием LLM."""

    def __init__(self, rag_pipeline=None):
        self.rag = rag_pipeline
        self.last_generation_meta: Dict[str, Any] = {}

        # Загружаем портфолио
        self.portfolio = self._load_portfolio()

        # Системный промпт для РУССКОГО — мини-решение вместо "могу сделать"
        self.system_prompt_ru = """
Ты — опытный фриланс-разработчик. Пишешь отклик, который ВЫДЕЛЯЕТСЯ среди шаблонных "могу сделать".
Стратегия: покажи что ты УЖЕ думаешь над задачей — предложи конкретный подход.

ПРАВИЛА (СТРОГО!):
1. ПРИВЕТСТВИЕ: Начинай с "Здравствуйте!". Единственно допустимое.
2. НИКАКИХ СМАЙЛИК И ЭМОДЗИ. Чистый профессиональный текст.
3. НИКАКОГО MARKDOWN И СПЕЦСИМВОЛОВ. Запрещено: *, #, _, -, списки. Только буквы и знаки препинания.
4. НИКАКОГО ВСТУПИТЕЛЬНОГО ТЕКСТА. Не пиши "Вот вариант", "Конечно". Сразу к делу.
5. СТРУКТУРА ОТКЛИКА (3-5 предложений):
   а) Здравствуйте!
   б) Конкретный подход к решению: какой стек/метод/архитектура ты бы использовал. 1-2 предложения.
   в) Релевантный опыт: ОДИН конкретный кейс из профиля, который ближе всего к задаче. 1 предложение.
   г) Технический вопрос по задаче, который покажет что ты вник. 1 предложение.
6. КОНКРЕТНОСТЬ: Не пиши "имею опыт в разработке". Пиши "для этой задачи подойдёт FastAPI + Celery для фоновой обработки".
7. ДЛИНА: 3-5 предложений. Не больше.
8. АБСОЛЮТНЫЙ ЗАПРЕТ: Никогда не упоминай в отклике: конкурентов, количество откликов, цены других исполнителей, OSINT-данные (GitHub/Habr/соцсети заказчика), его профиль, статистику найма, отзывы о нём. Все эти данные даны тебе ТОЛЬКО для настройки тона и выбора акцентов (более уверенный при низкой конкуренции, более детальный при высокой; если у заказчика есть GitHub — говори технически, если новичок — проще). Заказчик НЕ ДОЛЖЕН подозревать что тебе что-то известно о нём сверх описания проекта. НИКАКИХ фраз "я смотрел ваш профиль", "вижу что вы...", "нашёл ваш GitHub".
"""

        # Системный промпт для АНГЛИЙСКОГО — мини-решение
        self.system_prompt_en = """
You are a freelancer developer. Write a proposal that STANDS OUT from generic "I can do this" responses.
Strategy: show you ALREADY thought about the task — propose a concrete approach.

RULES (STRICT!):
1. NO MARKDOWN. Do not use *, #, _, -, or lists. Only plain text.
2. NO ASSISTANT FLUFF. Do not say "Sure!", "Here is your proposal". Start directly.
3. STRUCTURE (3-5 sentences):
   a) Greeting
   b) Concrete approach: what stack/method/architecture you'd use. 1-2 sentences.
   c) Relevant experience: ONE specific case from profile closest to the task. 1 sentence.
   d) Technical question that shows you read the description. 1 sentence.
4. BE SPECIFIC: Don't write "I have experience in development". Write "For this task, FastAPI + Celery for background processing would work well".
5. MAX 5 sentences.
6. ABSOLUTE PROHIBITION: Never mention competitors, number of offers, other freelancers' prices, or any competition data. This info is given to you ONLY to adjust your tone (more confident with low competition, more detailed with high competition). The client MUST NEVER know you have this information.
"""

    def _load_portfolio(self) -> Dict[str, Any]:
        """Загрузка портфолио для контекста."""
        if PORTFOLIO_FILE.exists():
            with PORTFOLIO_FILE.open("r", encoding="utf-8") as f:
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

    def _load_cases(self) -> list:
        """Загрузка кейсов из cases.json."""
        if CASES_FILE.exists():
            try:
                with CASES_FILE.open("r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return []

    def _pick_best_case(self, project: Any, cases: list) -> str:
        """Выбор наиболее релевантного кейса по пересечению технологий."""
        if not cases:
            return ""

        proj_text = f"{project.title} {project.description}".lower()
        proj_skills_set = set(s.lower() for s in (project.skills or []))

        best_case = None
        best_score = -1

        for case in cases:
            case_tech = set(t.lower() for t in case.get("tech", []))
            # Пересечение навыков проекта и кейса
            overlap = len(proj_skills_set & case_tech)
            # Совпадение слов в описании
            desc_words = set(case.get("description", "").lower().split())
            text_overlap = sum(1 for w in desc_words if len(w) > 4 and w in proj_text)
            score = overlap * 3 + text_overlap

            if score > best_score:
                best_score = score
                best_case = case

        if best_case and best_score > 0:
            return (
                f"Релевантный кейс: {best_case['title']}. "
                f"{best_case.get('description', '')} "
                f"Результат: {best_case.get('result', '')}"
            )
        return ""

    def _build_user_prompt(self, project: Any, lang: str = "ru",
                           client_data: dict = None, competitor_prices: list = None) -> str:
        """Сборка контекста заказа с профилем, кейсом, данными заказчика и конкуренцией."""
        proj_skills = ", ".join(project.skills) if project.skills else "указанные в описании"

        dev = self.portfolio.get("developer", {})
        my_skills = ", ".join(dev.get("skills", []))
        exp_years = dev.get("experience_years", 4)

        # RAG контекст
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

        # Лучший кейс из портфолио
        cases = self._load_cases()
        best_case = self._pick_best_case(project, cases)

        # Профиль заказчика (если есть из API)
        client_context = ""
        if client_data:
            parts = []
            if client_data.get("completed_orders_count", 0) > 0:
                parts.append(f"завершённых заказов: {client_data['completed_orders_count']}")
            if client_data.get("rating", 0) > 0:
                parts.append(f"рейтинг: {client_data['rating']}")
            if client_data.get("online"):
                parts.append("онлайн сейчас")
            if client_data.get("achievments_count", 0) > 0:
                parts.append(f"бейджей: {client_data['achievments_count']}")
            if client_data.get("order_done_repeat_persent", 0) > 0:
                parts.append(f"повторных заказов: {client_data['order_done_repeat_persent']}%")
            if client_data.get("good_reviews", 0) > 0:
                parts.append(f"положительных отзывов: {client_data['good_reviews']}")
            if client_data.get("bad_reviews", 0) > 0:
                parts.append(f"⚠️ негативных отзывов: {client_data['bad_reviews']}")
            if client_data.get("completed_orders_count", 0) == 0:
                parts.append("⚠️ НИКОГДА не нанимал исполнителей")
            if parts:
                client_context = "\nПРОФИЛЬ ЗАКАЗЧИКА: " + ", ".join(parts)

            # OSINT-контекст — только для настройки тона, не упоминать в отклике
            osint_lines = []
            if client_data.get("osint_positive"):
                osint_lines.append("плюсы: " + ", ".join(client_data["osint_positive"][:3]))
            if client_data.get("osint_red_flags"):
                osint_lines.append("риски: " + "; ".join(client_data["osint_red_flags"][:2]))
            if osint_lines:
                client_context += "\nOSINT-СИГНАЛЫ (для тона): " + " | ".join(osint_lines)

        # Конкуренция
        competition_context = ""
        offers = getattr(project, "offers_count", 0)
        if offers > 0:
            competition_context += f"\nКОНКУРЕНЦИЯ: {offers} откликов уже подано"
            if offers > 50:
                competition_context += " (очень высокая — нужно выделиться)"
            elif offers > 20:
                competition_context += " (средняя — нужен сильный отклик)"
        if competitor_prices:
            prices = [p["price"] for p in competitor_prices if "price" in p]
            if prices:
                competition_context += f"\nЦЕНЫ КОНКУРЕНТОВ: мин {min(prices)}₽, макс {max(prices)}₽, среднее {sum(prices)//len(prices)}₽"

        title_safe = self._sanitize_text(project.title)
        desc_safe = self._sanitize_text(project.description)

        if lang == "en":
            case_section = f"\nMY BEST MATCH: {best_case}" if best_case else ""
            return (
                f"PROJECT: {title_safe}\n"
                f"DESC: {desc_safe}\n"
                f"SKILLS NEEDED: {proj_skills}\n"
                f"MY PROFILE: {rag_context}"
                f"{case_section}"
                f"{client_context}"
                f"{competition_context}\n"
                f"Write a proposal with a concrete approach and technical question."
            )
        else:
            case_section = f"\nМОЙ РЕЛЕВАНТНЫЙ КЕЙС: {best_case}" if best_case else ""
            return (
                f"ЗАКАЗ: {title_safe}\n"
                f"ОПИСАНИЕ: {desc_safe}\n"
                f"НАВЫКИ ЗАКАЗЧИКА: {proj_skills}\n"
                f"МОЙ ПРОФИЛЬ: {rag_context}"
                f"{case_section}"
                f"{client_context}"
                f"{competition_context}\n"
                f"Напиши отклик с конкретным подходом к решению и техническим вопросом."
            )

    async def generate(self, project: Any, provider: Optional[str] = None,
                       client_data: dict = None, competitor_prices: list = None) -> str:
        """Основной метод генерации с использованием LLM Router (Groq/Google/GLM)."""
        from src.brain.llm_router import get_llm_router

        lang = "ru"

        system_prompt = self.system_prompt_en if lang == "en" else self.system_prompt_ru
        prompt = self._build_user_prompt(project, lang, client_data=client_data,
                                         competitor_prices=competitor_prices)

        with ensure_parent(LAST_LLM_PROMPT_FILE).open("w", encoding="utf-8") as f:
            f.write(f"SYSTEM:\n{system_prompt.strip()}\n\nUSER:\n{prompt}")

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
                prompt=prompt,
                provider=provider if provider != "auto" else None,
                model=model,
                temperature=0.75,
                max_tokens=500,
                task="proposal_writing",
                system_prompt=system_prompt,
            )
            self.last_generation_meta = router.get_last_route()

            if res:
                res = self._clean_llm_response(res)
                with ensure_parent(LAST_LLM_RESPONSE_FILE).open("w", encoding="utf-8") as f:
                    f.write(res)
                return res

        except Exception as e:
            logger.error(f"LLM Router Error ({provider}): {e}")
            self.last_generation_meta = {"provider": "template_fallback", "model": "template", "task": "proposal_writing"}

        # Fallback на шаблоны
        logger.warning(f"Все LLM недоступны, используем шаблоны для отклика на {project.title}")
        return generate_template_proposal(
            {"title": project.title, "found_skills": project.skills, "budget": project.budget}, self.portfolio
        )


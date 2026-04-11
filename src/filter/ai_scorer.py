"""
AI-скоринг релевантности проектов.
Использует LLM для оценки, подходит ли проект фрилансеру.
Двухступенчатая фильтрация: сначала keyword-фильтр, потом AI-скоринг.
"""

import os
import json
import re
from typing import List, Dict, Any, Tuple
from loguru import logger
from groq import Groq
from src.parsers.base_parser import ProjectItem


class AIRelevanceScorer:
    """Оценивает релевантность проектов через LLM."""

    def __init__(self, portfolio_path: str = "data/portfolio.json"):
        self.groq_api_key = os.getenv("GROQ_API_KEY")
        self.groq_client = None
        if self.groq_api_key:
            try:
                self.groq_client = Groq(api_key=self.groq_api_key)
            except Exception as e:
                logger.error(f"AIScorer: не удалось инициализировать Groq: {e}")

        self.portfolio = self._load_portfolio(portfolio_path)
        self._build_profile_summary()

    def _load_portfolio(self, path: str) -> Dict[str, Any]:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        return {}

    def _build_profile_summary(self):
        """Собираем краткий профиль для промпта."""
        dev = self.portfolio.get("developer", {})
        skills = dev.get("skills", [])
        highlights = self.portfolio.get("portfolio_highlights", [])

        self.profile = (
            f"Навыки: {', '.join(skills)}. "
            f"Опыт: {dev.get('experience_years', 3)} лет. "
            f"Примеры работ: {'; '.join(highlights[:3]) if highlights else 'веб-разработка, автоматизация'}."
        )

    async def score_projects(
        self,
        projects: List[ProjectItem],
        threshold: int = 6,
    ) -> List[Tuple[ProjectItem, int]]:
        """
        Оценивает список проектов через LLM.
        Возвращает только те, что набрали >= threshold баллов.
        """
        if not self.groq_client:
            logger.warning("AIScorer: Groq не инициализирован, пропускаем AI-скоринг")
            return [(p, 7) for p in projects]  # fallback: всё проходит

        if not projects:
            return []

        # Батчим по 10 проектов для эффективности
        scored = []
        for i in range(0, len(projects), 10):
            batch = projects[i:i + 10]
            batch_scores = await self._score_batch(batch)
            scored.extend(batch_scores)

        # Фильтруем по порогу
        passed = [(p, score) for p, score in scored if score >= threshold]
        rejected = [(p, score) for p, score in scored if score < threshold]

        if rejected:
            logger.info(
                f"AIScorer: отсеяно {len(rejected)} нерелевантных проектов: "
                + ", ".join([f"'{p.title[:30]}' ({s}/10)" for p, s in rejected[:5]])
            )

        logger.info(f"AIScorer: прошло AI-скоринг: {len(passed)}/{len(scored)}")
        return passed

    async def _score_batch(
        self, batch: List[ProjectItem]
    ) -> List[Tuple[ProjectItem, int]]:
        """Оценивает батч проектов одним запросом к LLM."""
        # Формируем список проектов для промпта
        projects_text = ""
        for idx, p in enumerate(batch, 1):
            desc_short = (p.description or "")[:200]
            skills_str = ", ".join(p.skills[:5]) if p.skills else "не указаны"
            projects_text += (
                f"{idx}. [{p.platform}] {p.title}\n"
                f"   Описание: {desc_short}\n"
                f"   Навыки: {skills_str}\n"
                f"   Бюджет: {p.budget or 'не указан'}\n\n"
            )

        prompt = f"""Ты — ассистент, который оценивает релевантность фриланс-проектов для разработчика.

ПРОФИЛЬ РАЗРАБОТЧИКА:
{self.profile}

КРИТЕРИИ ОЦЕНКИ (0-10):
- 9-10: Идеальное совпадение навыков, это заказ на разработку/автоматизацию, который один человек может выполнить
- 7-8: Хорошее совпадение, некоторые навыки совпадают, реальный фриланс-заказ
- 5-6: Частичное совпадение, но заказ сомнительный или слишком крупный для одного человека
- 3-4: Слабое совпадение, корпоративная вакансия (штатная позиция, а не фриланс)
- 1-2: Не подходит вообще (научная работа, военка, дизайн, хардвер, не IT)
- 0: Спам или мусор

ВАЖНО: Штатные вакансии (full-time, офис, "Инженер-интегратор") — это НЕ фриланс. Ставь им 2-3.
ВАЖНО: "Data Scientist", "ML Engineer" с PhD — это НЕ подходит разработчику на Python/Django. Ставь 1-3.

СПИСОК ПРОЕКТОВ:
{projects_text}

Ответь СТРОГО в формате JSON массива, без markdown:
[{{"id": 1, "score": 8}}, {{"id": 2, "score": 3}}, ...]
Только JSON, ничего больше!"""

        try:
            completion = self.groq_client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=[
                    {"role": "system", "content": "Ты оцениваешь релевантность проектов. Отвечай ТОЛЬКО валидным JSON."},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.1,  # Низкая температура для стабильности оценок
                max_tokens=300,
            )

            response_text = completion.choices[0].message.content.strip()
            # Извлекаем JSON из ответа
            json_match = re.search(r'\[.*\]', response_text, re.DOTALL)
            if not json_match:
                logger.warning(f"AIScorer: не удалось извлечь JSON: {response_text[:100]}")
                return [(p, 7) for p in batch]  # fallback

            scores_data = json.loads(json_match.group())

            results = []
            for idx, project in enumerate(batch):
                score = 7  # default
                for item in scores_data:
                    if item.get("id") == idx + 1:
                        score = max(0, min(10, int(item.get("score", 7))))
                        break
                results.append((project, score))

            return results

        except Exception as e:
            logger.error(f"AIScorer: ошибка при скоринге: {e}")
            return [(p, 7) for p in batch]  # fallback: всё проходит

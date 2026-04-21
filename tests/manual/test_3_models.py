"""
Тест генерации 3 откликов с ротацией моделей
"""

import asyncio
import os
import sys
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.brain.llm_router import get_llm_router
from src.action.proposal_templates import generate_proposal as generate_template_proposal

# Тестовые проекты
test_projects = [
    {
        "title": "Парсер недвижимости Avito/Cian",
        "description": "Сбор цен, адресов, площади. Ежедневное обновление. Excel выгрузка.",
        "skills": ["python", "парсинг"],
        "budget": 5000,
    },
    {
        "title": "Бот для Telegram на Python",
        "description": "Автоматическая публикация постов, сбор статистики, интеграция с API.",
        "skills": ["python", "telegram", "bot"],
        "budget": 8000,
    },
    {
        "title": "Скрейпинг данных с сайта",
        "description": "Нужно собрать каталог товаров: название, цена, описание, фото. 5000 позиций.",
        "skills": ["python", "scraping"],
        "budget": 10000,
    },
]

system_prompt = """
Ты — профессиональный разработчик. Напиши короткий отклик заказчику.
ПРАВИЛА:
1. Начни с "Здравствуйте!"
2. Без смайликов и markdown
3. 3-4 предложения
4. Упомяни опыт
5. Задай вопрос
"""

providers = ["groq", "google", "glm"]


async def test_generation():
    print("\n" + "=" * 60)
    print("ТЕСТ: 3 ОТКЛИКА С РАЗНЫМИ МОДЕЛЯМИ")
    print("=" * 60)

    router = get_llm_router()

    for i, project in enumerate(test_projects):
        provider = providers[i % len(providers)]
        print(f"\n{'=' * 60}")
        print(f"[{i + 1}/3] Проект: {project['title']}")
        print(f"Модель: {provider}")
        print(f"{'=' * 60}")

        prompt = f"""
ЗАКАЗ: {project["title"]}
ОПИСАНИЕ: {project["description"]}
БЮДЖЕТ: {project["budget"]} руб.
"""

        try:
            import time

            start = time.time()

            # Выбираем модель
            if provider == "groq":
                model = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
            elif provider == "google":
                model = os.getenv("GOOGLE_MODEL", "gemini-2.5-flash")
            else:
                model = os.getenv("GLM_MODEL", "glm-4.6v")

            result = await router.generate(
                prompt=f"{system_prompt}\n\n{prompt}",
                provider=provider,
                model=model,
                temperature=0.75,
                max_tokens=300,
            )

            elapsed = time.time() - start

            print(f"Время: {elapsed:.2f} сек | Длина: {len(result)} символов")
            print(f"Ответ:\n{result}")

        except Exception as e:
            print(f"ОШИБКА: {e}")

    print("\n" + "=" * 60)
    print("ТЕСТ ЗАВЕРШЕН")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(test_generation())

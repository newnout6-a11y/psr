"""
Шаблоны сопроводительных писем (fallback без LLM).
Контекстно подставляются навыки, бюджет, детали проекта.
"""

import random
from typing import Dict, Any

TEMPLATES = [
    # Шаблон 1: Прямой и прагматичный
    """Приветствую. Посмотрел ваш проект «{title}» — задача понятна, стек ({skills}) мой основной.

Есть опыт в аналогичных проектах — делал {case_example}. Разбираюсь в предметной области, так что не будет долгого погружения.

По срокам: ориентируюсь на {timeline}. По бюджету — укладываюсь в {budget}.

Если готовы обсудить детали — напишите, задам уточняющие вопросы по ТЗ.""",
    # Шаблон 2: С акцентом на опыт
    """Здравствуйте! Ваш проект «{title}» попал в мою зону компетенции.

Почему я:
- {years} лет работаю с {skills}
- Реализовал {projects_count}+ аналогичных проектов
- Последний похожий: {case_example}

Предлагаю начать с короткого созвона/переписки — обсудим нюансы. Если задача интересна, возьмусь на этой неделе.""",
    # Шаблон 3: Короткий и по делу
    """Привет. Вижу задачу: {title}. Это как раз мой профиль — {skills}.

Готов приступить. Примеры похожих работ:
- {case_1}
- {case_2}

Бюджет и сроки обсуждаемы. Жду ответа.""",
    # Шаблон 4: С вопросом
    """Добрый день! Заинтересовал ваш проект «{title}».

Пара уточнений перед стартом:
1. {question_1}
2. {question_2}

О себе: {skills} — основной стек, {years} лет опыта. Последний проект в этой области — {case_example}.

Готов обсудить после того, как проясним эти моменты.""",
    # Шаблон 5: Технический
    """Приветствую. По проекту «{title}»:

Техническое видение:
- Стек: {skills}
- Подход: {approach}
- Сроки: {timeline}

Опыт: {years} лет коммерческой разработки. Из недавнего — {case_example}.

Если подход устраивает — готов начинать.""",
]

QUESTIONS = {
    "python": [
        "Есть ли готовое ТЗ или обсуждаем архитектуру?",
        "Какой фреймворк предпочтителен — Django, FastAPI, Flask?",
    ],
    "javascript": ["Нужен фронтенд или fullstack?", "React/Vue или чистый JS?"],
    "wordpress": ["Это кастомная тема или готовая с доработками?", "Нужна интеграция с чем-то?"],
    "api": ["Есть документация API?", "REST или GraphQL?"],
    "telegram bot": ["Какой функционал ожидается?", "Есть сервер или нужен хостинг?"],
    "html": ["Это лендинг или многостраничник?", "Дизайн готовый или тоже нужен?"],
    "css": ["Есть макет в Figma/PSD?", "Нужна адаптивная вёрстка?"],
    "sql": ["Какая СУБД?", "Нужна оптимизация или разработка с нуля?"],
    "docker": ["Это контейнеризация существующего приложения?", "Нужен docker-compose или оркестрация?"],
    "devops": ["Какая текущая инфраструктура?", "Нужен CI/CD или только настройка серверов?"],
    "android": ["Native или кроссплатформа?", "Есть дизайн/макеты?"],
    "ai": ["Какая модель/фреймворк?", "Есть обучающие данные?"],
    "default": ["Есть ли готовое ТЗ?", "Когда планируете стартовать?"],
}

APPROACHES = {
    "python": "Python + оптимальный фреймворк под задачу, покрытие тестами, документация",
    "javascript": "Современный JS/TS, модульная архитектура, чистый код",
    "wordpress": "Кастомная тема, оптимизация производительности, безопасность",
    "html": "Семантическая вёрстка, адаптивность, кроссбраузерность",
    "css": "Mobile-first, CSS-переменные, минимум зависимостей",
    "api": "RESTful/GraphQL архитектура, валидация, документация Swagger",
    "docker": "Мультистейдж билды, минимальные образы, docker-compose",
    "sql": "Нормализация, индексы, оптимизация запросов, миграции",
    "default": "Чистый код, документация, покрытие тестами",
}

TIMELINES = {
    "easy": "3-5 дней",
    "medium": "1-2 недели",
    "hard": "2-4 недели",
}


def generate_proposal(project: Dict[str, Any], portfolio: Dict[str, Any]) -> str:
    """
    Генерация сопроводительного письма без LLM.
    Использует шаблоны + контекст проекта и портфолио.
    """
    title = project.get("title", "")
    skills = project.get("found_skills", [])
    budget = project.get("budget")
    complexity = project.get("complexity", "medium")

    dev_info = portfolio.get("developer", {})
    cases = portfolio.get("cases", portfolio.get("portfolio_highlights", []))
    years = dev_info.get("experience_years", 5)

    # Форматируем навыки
    skills_str = ", ".join(skills[:3]) if skills else "указанный стек"

    # Берём случайный кейс
    if isinstance(cases, list) and len(cases) > 0:
        case_example = cases[0].get("title", "аналогичный проект") if isinstance(cases[0], dict) else str(cases[0])
        case_1 = cases[0].get("title", "") if isinstance(cases[0], dict) else str(cases[0])
        case_2 = cases[1].get("title", "") if len(cases) > 1 and isinstance(cases[1], dict) else ""
    else:
        case_example = "аналогичный проект"
        case_1 = "портфолио доступно по запросу"
        case_2 = ""

    # Бюджет
    budget_str = f"{budget} {project.get('currency', '')}" if budget else "обсуждаемо"

    # Сроки
    timeline = TIMELINES.get(complexity, "1-2 недели")

    # Вопросы
    matched_questions = QUESTIONS.get("default", [])
    for skill in skills:
        if skill in QUESTIONS:
            matched_questions = QUESTIONS[skill]
            break
    question_1 = matched_questions[0] if len(matched_questions) > 0 else "Есть ли ТЗ?"
    question_2 = matched_questions[1] if len(matched_questions) > 1 else "Когда планируете старт?"

    # Подход
    approach = APPROACHES.get("default")
    for skill in skills:
        if skill in APPROACHES:
            approach = APPROACHES[skill]
            break

    # Выбираем случайный шаблон
    template = random.choice(TEMPLATES)

    # Подставляем значения
    result = template.format(
        title=title[:80],
        skills=skills_str,
        case_example=case_example,
        case_1=case_1,
        case_2=case_2,
        budget=budget_str,
        timeline=timeline,
        years=years,
        projects_count=random.randint(10, 50),
        question_1=question_1,
        question_2=question_2,
        approach=approach,
    )

    return result

from src.brain.search_strategy import SearchStrategy


def test_merge_queries_limits_semantic_duplicates():
    merged = SearchStrategy._merge_queries(
        preferred_queries=[],
        learned_queries=[],
        ai_queries=[
            "скрипты python",
            "боты для телеграм",
            "телеграм бот",
            "автоматизация бизнеса",
            "скрипт python",
            "бот для телеграм",
            "автоматизация задач",
            "python скрипты",
        ],
        negative_queries=set(),
        platform="kwork",
        count=8,
    )

    topics = [SearchStrategy._query_topic_key(query) for query in merged]

    assert len(merged) == 8
    assert topics.count("telegram_bot") == 1
    assert topics.count("python_script") == 1
    assert "парсер сайтов" in merged
    assert "api интеграция" in merged
    assert "fastapi backend" in merged


def test_merge_queries_caps_topics_when_count_is_large():
    merged = SearchStrategy._merge_queries(
        preferred_queries=[],
        learned_queries=[],
        ai_queries=[
            "боты для телеграм",
            "телеграм бот",
            "бот для телеграм",
            "телеграм бот python",
            "скрипты python",
            "скрипт python",
            "python скрипты",
            "python скрипт",
        ],
        negative_queries=set(),
        platform="kwork",
        count=20,
    )

    topics = [SearchStrategy._query_topic_key(query) for query in merged]

    assert topics.count("telegram_bot") <= 2
    assert topics.count("python_script") <= 2
    assert "n8n автоматизация" in merged
    assert "bitrix24 api" in merged
    assert "webhook интеграция" in merged


def test_frontend_design_brief_overrides_python_memory():
    merged = SearchStrategy._merge_queries(
        preferred_queries=["скрипты python", "телеграм бот"],
        learned_queries=["парсер сайтов", "api интеграция"],
        ai_queries=["python скрипты", "бот для телеграм"],
        negative_queries=set(),
        platform="kwork",
        count=8,
        user_brief="фронтенд, дизайн сайта, дизайн предложений",
    )

    topics = [SearchStrategy._query_topic_key(query) for query in merged]

    assert "верстка сайта" in merged
    assert "frontend react" in merged
    assert "дизайн сайта" in merged
    assert "скрипты python" not in merged
    assert "телеграм бот" not in merged
    assert "frontend" in topics
    assert "design" in topics

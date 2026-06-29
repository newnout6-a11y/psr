# PSR

Единый актуальный README проекта.

Актуально на: `2026-04-21`

Этот файл теперь является единственным источником истины по проекту. Старые `README`, usage-документы и текстовые аудиты удалены.

## Что это

PSR это Python-проект для автоматизации цикла поиска и обработки заказов на фриланс-биржах:

- парсинг заказов с поддерживаемых площадок;
- keyword/NLP/AI-фильтрация;
- веттинг заказчика;
- OSINT-обогащение;
- генерация отклика;
- ручное подтверждение через Telegram;
- авто-отправка там, где она реализована;
- логирование в SQLite и просмотр метрик в Streamlit.

## Что сейчас реально работает

- `kwork`:
  поиск через API, fallback на браузер, генерация отклика, ручное подтверждение цены, авто-отправка;
- `freelance_ru`:
  HTML-поиск, генерация отклика, ручное подтверждение цены, browser-flow для отправки;
- `hh_ru`:
  поиск через публичное API, используется как источник вакансий/заказов для мониторинга, без авто-отправки;
- `Groq`:
  основной и фактически рабочий LLM-провайдер в текущем коде;

## Структура проекта

```text
psr/
├── config/
│   ├── filters.yaml      # Рабочие фильтры рантайма
│   └── platforms.yaml    # Справочное описание платформ
├── data/
│   ├── reference/        # Портфолио, кейсы, поисковые reference-данные
│   ├── runtime/          # БД, логи, browser profiles, parsing results
│   └── debug/            # HTML-дампы, скриншоты и ручной дебаг
├── scripts/
│   ├── analyze/          # Диагностические и проверочные скрипты
│   ├── debug/            # Ручной дебаг платформ и страниц
│   ├── session_hub/      # Локальные Session Hub серверы
│   └── check_client.py   # Ручной OSINT/пробив клиента
├── src/
│   ├── action/          # Генерация и отправка откликов
│   ├── brain/           # LLM router, RAG, NLP, search strategy
│   ├── browser/         # BrowserManager, fingerprint, auth/cookies
│   ├── dashboard/       # Streamlit-дашборд
│   ├── evolution/       # TLS client и вспомогательные сетевые модули
│   ├── filter/          # Keyword filter, AI scorer, vetting
│   ├── osint/           # OSINT и probiv-агрегатор
│   ├── parsers/         # Парсеры платформ
│   ├── paths.py         # Единая карта путей проекта
│   ├── utils/           # Telegram, логи, валюты, circuit breaker, время
│   └── orchestrator.py  # Главный цикл
├── tests/
│   ├── smoke/           # Быстрые автономные проверки
│   ├── integration/     # Сквозные проверки модулей
│   └── manual/          # Ручные инженерные тесты
├── scratch/             # Локальные черновики, в git не идут
├── main.py              # Точка входа
├── py.ps1               # Обёртка для правильного Python на этой машине
├── requirements.txt
└── .env.example
```

Логика по папкам простая:

- `data/reference` хранит входные данные, которые нужны проекту постоянно;
- `data/runtime` хранит всё, что появляется во время работы;
- `data/debug` собирает только отладочные артефакты, чтобы они не смешивались с рантаймом и исходниками.

## Окружение на этой машине

На этой машине корректный Python расположен здесь:

```text
C:\Users\Redmi\AppData\Local\Programs\Python\Python312\python.exe
```

Для удобства в репозитории есть `py.ps1`:

```powershell
.\py.ps1 main.py --dry-run --limit 3
.\py.ps1 scripts\check_client.py torvalds
.\py.ps1 -m pip install -r requirements.txt
```

Если понадобится `node`/`npm`, путь на этой машине:

```text
C:\Program Files\nodejs
```

Сам проект при обычной работе остаётся Python-first; обязательного Node toolchain для основного цикла нет.

## Зависимости

Основные зависимости берутся из [requirements.txt](/C:/psr/requirements.txt):

- сетевой слой: `requests`, `curl_cffi`, `httpx`, `aiohttp`;
- браузер: `nodriver`, `playwright`;
- парсинг: `lxml`, `parsel`, `beautifulsoup4`, `pydantic`;
- LLM/NLP: `groq`, `sentence-transformers`, `faiss-cpu`;
- Telegram и утилиты: `aiogram`, `telethon`, `python-dotenv`, `pyyaml`, `loguru`, `tenacity`, `rich`;
- наблюдаемость: `streamlit`, `pandas`.

Установка:

```powershell
.\py.ps1 -m pip install -r requirements.txt
```

## Конфигурация

1. Создай `.env` из шаблона:

```powershell
Copy-Item .env.example .env
```

2. Заполни минимум:

```env
GROQ_API_KEY=
PLATFORMS=kwork,freelance_ru,hh_ru
SEARCH_BRIEF=мелкие заказы на автоматизацию, ботов, парсеры, скрипты, небольшие сайты. бюджет до 10к. не на постоянку, не крупные проекты
QUERY_COUNT=3
PAGES_TO_PARSE=1
TELEGRAM_TOKEN=
ADMIN_CHAT_ID=
TELEGRAM_TRANSPORT=auto
```

3. Ключевые переменные:

- `GROQ_API_KEY`: основной рабочий LLM;
- `AI_SCORE_THRESHOLD`: порог AI-скоринга;
- `PLATFORMS`: активные платформы;
- `SEARCH_BRIEF`: описание поиска своими словами;
- `SEARCH_QUERY`: fallback-запросы через `|`, если AI генерация недоступна;
- `QUERY_COUNT`: сколько AI-запросов генерировать на платформу;
- `PAGES_TO_PARSE`: сколько страниц парсить по каждому запросу;
- `TOP_PROJECTS`: сколько лучших проектов обрабатывать в цикле;
- `CONTINUOUS_MODE` и `CYCLE_INTERVAL`: непрерывный режим;
- `TELEGRAM_TOKEN`, `ADMIN_CHAT_ID`, `APPROVAL_TIMEOUT`: approval и уведомления;
- `TELEGRAM_TRANSPORT`: `auto`, `bot_api` или `mtproto`;
- `TELEGRAM_BOT_API_BASE`: Bot API endpoint, по умолчанию `https://api.telegram.org`;
- `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`: опциональный MTProto fallback без `api.telegram.org`;
- `BROWSER_HEADLESS`: headless браузер;
- `SESSION_HUB_URL`, `SESSION_HUB_REQUIRED`: источник кук;
- `OSINT_ENABLED` и `OSINT_*`: OSINT и probiv;
- `PROXY_URL`: прокси для API и сетевых вызовов.

Дополнительные фильтры проекта живут в [config/filters.yaml](/C:/psr/config/filters.yaml). В рантайме реально применяются:

- `required_skills`;
- `stop_words`;
- `min_budget`;
- `max_budget`;
- `per_page`;
- `max_age_hours`;
- `max_proposals`;
- `min_client_score`.

## Session Hub и куки

В текущем коде Session Hub это основной источник кук для браузерной авторизации.

Приоритет такой:

1. `SESSION_HUB_URL`
2. куки из `.env` как fallback, если `SESSION_HUB_REQUIRED=false`

### Реальные скрипты в репозитории

- `scripts/session_hub/session_hub_manual.py`
  простой локальный сервер, который хранит куки в `session_hub_cookies.json`;
- `scripts/session_hub/session_hub_example.py`
  пример сервера, читающего куки из Chrome через `browser-cookie3`.

### Важно

Эти скрипты требуют дополнительные зависимости, которых нет в `requirements.txt` основного проекта:

```powershell
.\py.ps1 -m pip install flask
```

Для `session_hub_example.py` дополнительно:

```powershell
.\py.ps1 -m pip install browser-cookie3
```

### Запуск manual Session Hub

```powershell
.\py.ps1 scripts\session_hub\session_hub_manual.py
```

После этого сервер слушает `http://127.0.0.1:8669`.

Поддерживаемые endpoint'ы:

- `GET /cookies?domain=kwork.ru`
- `POST /update`
- `GET /list`
- `GET /health`

### Fallback через `.env`

Для `kwork`:

```env
KWORK_EMAIL=
KWORK_PASSWORD=
KWORK_COOKIE_REMEMBERME=
KWORK_COOKIE_USERID=
KWORK_COOKIE_PHPSESSID=
KWORK_COOKIE_CSRF=
```

Для `freelance_ru`:

```env
FREELANCE_RU_COOKIES_JSON=
FREELANCE_RU_COOKIE_SESSION=
FREELANCE_RU_COOKIE_DUID=
FREELANCE_RU_COOKIE_REMEMBER=
```

## Запуск проекта

### Один цикл

```powershell
.\py.ps1 main.py
```

### Без реальной отправки

```powershell
.\py.ps1 main.py --dry-run --limit 3
```

### С произвольным поисковым описанием

```powershell
.\py.ps1 main.py --dry-run --search "парсеры, telegram-боты, автоматизация, мелкие backend-заказы" --top 5
```

### Непрерывный режим

```powershell
.\py.ps1 main.py --continuous --limit 5
```

Аргументы `main.py`:

- `--continuous`: бесконечный цикл;
- `--dry-run`: без реальной отправки;
- `--limit`: лимит откликов на платформу за цикл;
- `--search`: описание поиска своими словами;
- `--top`: сколько top-проектов брать в обработку.

## Как проходит один цикл

1. Загрузка `.env`
2. Инициализация `FreelanceOrchestrator`
3. Проверка входящих сообщений
4. Генерация поисковых запросов
5. Парсинг платформ
6. Keyword-фильтрация
7. NLP-фильтрация
8. AI-скоринг
9. Веттинг заказчика
10. OSINT-обогащение
11. Генерация отклика
12. Telegram approval по цене и тексту
13. Авто-отправка или сохранение как draft
14. Логирование в SQLite

## Telegram-flow

Telegram используется для двух задач:

- approval по цене и тексту отклика;
- уведомления о входящих ответах от заказчика.

В текущей версии:

- approval идёт через inline-кнопки;
- можно approve/skip, отложить кандидата, править текст и цену, помечать похожий запрос как полезный;
- можно отправить новый текст отклика;
- уведомления отправляются в plain text без markdown-экранирования, чтобы не ломаться на пользовательских строках.
- доставка уведомлений идёт через прямой Bot API без системных proxy; если задан `TELEGRAM_API_ID`/`TELEGRAM_API_HASH`, включается MTProto fallback через Telegram DC.

## OSINT и probiv

OSINT включается через:

```env
OSINT_ENABLED=true
OSINT_PROVIDERS=github,habr,duckduckgo,kwork_profile
OSINT_PROBIV_PROVIDERS=
```

Поддерживаются:

- публичные провайдеры: GitHub, Habr, DuckDuckGo, профиль Kwork;
- probiv-провайдеры: EmailRep, WhatsMyName, HIBP, LeakCheck, IntelX, но они отключены по умолчанию из-за задержек;
- извлечение email, телефонов, Telegram и username из текста проекта;
- кэширование результатов.

Ручной запуск проверки клиента:

```powershell
.\py.ps1 scripts\check_client.py torvalds
.\py.ps1 scripts\check_client.py suspicious_user -d "Пишите на test@example.com или @tg_nick"
```

## Dashboard

Запуск:

```powershell
streamlit run src\dashboard\app.py
```

Разделы:

- `Обзор`
- `Парсинг`
- `Генерация`
- `Отклики`
- `Ошибки`
- `Устойчивость`

Dashboard читает данные из:

- `data/logs.db`
- `data/proposals.db`

## Desktop / API

В проекте есть локальный FastAPI backend и Electron/React UI:

```powershell
# API
.\py.ps1 -m uvicorn src.api.server:app --host 127.0.0.1 --port 7788

# Desktop dev
cd desktop
npm ci
npm run dev
```

Desktop сам поднимает Python backend при запуске Electron. Кнопка запуска цикла в UI по умолчанию работает в `dry-run`; реальную отправку нужно включать явно.
Для обычного запуска больше не нужно править `.env`: левая панель Desktop передаёт в backend платформы, описание поиска, `QUERY_COUNT`, `PAGES_TO_PARSE`, `TOP_PROJECTS`, лимит отправок, режим Chrome, Telegram, OSINT, probiv и строгий Session Hub на конкретный цикл.

Важные детали:

- настройки UI читают и пишут проектные `.env` и `config/filters.yaml`;
- runtime-переключатели в левой панели применяются процессно на время запуска и не перетирают секреты в `.env`;
- секретные значения в Settings API маскируются и не отдаются в UI открытым текстом;
- `desktop/node_modules`, `desktop/dist` и `desktop/dist-electron` являются локальными артефактами и не коммитятся.

## Базы и рабочие файлы

Основные runtime-файлы в `data/`:

- `logs.db`: структурированные логи;
- `proposals.db`: отправленные и сохранённые отклики;
- `generated_proposals.txt`: последний накопленный дамп сгенерированных откликов;
- `last_llm_prompt.txt`
- `last_llm_response.txt`
- `browser_profiles/`: постоянный браузерный профиль;
- `osint_cache.db`: если OSINT кэш уже был создан во время работы.

Часть файлов в `data/` являются временными/служебными и не считаются документацией.

## Важные модули

- [main.py](/C:/psr/main.py): точка входа
- [src/orchestrator.py](/C:/psr/src/orchestrator.py): главный workflow
- [src/action/proposal_generator.py](/C:/psr/src/action/proposal_generator.py): генерация откликов
- [src/action/proposal_sender.py](/C:/psr/src/action/proposal_sender.py): отправка откликов
- [src/browser/browser_manager.py](/C:/psr/src/browser/browser_manager.py): браузер, авторизация, куки
- [src/filter/project_filter.py](/C:/psr/src/filter/project_filter.py): runtime-фильтрация
- [src/filter/client_vetter.py](/C:/psr/src/filter/client_vetter.py): веттинг клиента
- [src/brain/llm_router.py](/C:/psr/src/brain/llm_router.py): роутинг LLM
- [src/osint/aggregator.py](/C:/psr/src/osint/aggregator.py): OSINT aggregation
- [src/dashboard/app.py](/C:/psr/src/dashboard/app.py): дашборд

## Тесты и проверки

Локально можно запускать:

```powershell
.\py.ps1 tests\smoke\test_antifragile.py
.\py.ps1 tests\smoke\test_probiv.py
.\py.ps1 tests\integration\test_osint_integration.py
```

Синтаксическая проверка:

```powershell
.\py.ps1 -m compileall -q main.py src tests
```

Если на интерпретаторе установлен `pytest`, можно запускать и так:

```powershell
.\py.ps1 -m pytest -q
```

## Ограничения и текущее состояние

- основные LLM сейчас: `DeepSeek`, `OpenAI-compatible` и резервно `Groq`;
- `hh_ru` не отправляет отклики автоматически;
- `platforms.yaml` не является главным runtime-конфигом; рабочие настройки берутся из `.env` и `config/filters.yaml`;
- browser automation зависит от актуальных кук;
- `session_hub_*` скрипты являются локальной инфраструктурой и требуют отдельных пакетов;
- часть `scripts/analyze` и `scripts/debug` это инженерные вспомогательные инструменты, не обязательные для основного запуска.

## Быстрые команды

```powershell
# Установка зависимостей
.\py.ps1 -m pip install -r requirements.txt

# Dry-run
.\py.ps1 main.py --dry-run --limit 3

# Continuous
.\py.ps1 main.py --continuous

# OSINT-check
.\py.ps1 scripts\check_client.py torvalds

# Dashboard
streamlit run src\dashboard\app.py
```

## История документации

На `2026-04-21` из репозитория удалены разрозненные и устаревшие документы:

- старый `README.md` заменён этим файлом;
- `docs/USAGE.md`;
- `docs/SESSION_HUB.md`;
- текстовые research/audit-файлы в `docs/research/`;
- текстовый audit-report в `data/parsing_results/`.

Дальше поддерживается только этот `README.md`.

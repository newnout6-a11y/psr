# 🤖 Система автоматизации фриланс-парсинга и откликов

Полностью автономный комплекс для агрегации заказов с 35+ фриланс-платформ, ИИ-фильтрации и персонализированных откликов.

## 📁 Структура проекта

```
psr/
├── src/
│   ├── parsers/           # Парсеры платформ
│   │   ├── base_parser.py
│   │   ├── kwork_parser.py
│   │   ├── flru_parser.py
│   │   └── upwork_parser.py
│   ├── evolution/         # Обход WAF + реверс API
│   │   ├── tls_client.py
│   │   ├── origin_finder.py
│   │   └── api_reverser.py
│   ├── brain/             # NLP + RAG
│   │   ├── nlp_filter.py
│   │   └── rag_pipeline.py
│   ├── action/            # Генерация и отправка
│   │   ├── proposal_generator.py
│   │   └── proposal_sender.py
│   └── orchestrator.py    # Главный оркестратор
├── config/
│   └── platforms.yaml     # Конфигурация платформ
├── data/
│   ├── portfolio.json     # Портфолио разработчика
│   └── cases.json         # Кейсы для RAG
├── requirements.txt
├── main.py
└── .env.example
```

## 🚀 Быстрый старт

### 1. Установка зависимостей

```bash
pip install -r requirements.txt
```

### 2. Настройка

```bash
cp .env.example .env
# Заполните .env своими данными
```

#### Session Hub (рекомендуется)

Теперь **Session Hub** является основным источником кук для авторизации. Это локальный сервер (порт 8669), который предоставляет свежие куки из Chrome.

```bash
# Запустите Session Hub сервер (см. docs/SESSION_HUB.md)
# Или используйте куки из .env как fallback
```

См. полную документацию: [`docs/SESSION_HUB.md`](docs/SESSION_HUB.md)

### Основные переменные окружения

```bash
# LLM
GROQ_API_KEY=your_key
GOOGLE_API_KEY=your_key
LLM_PROVIDER=groq  # или google

# Платформы
PLATFORMS=kwork,fl_ru,fiverr
SEARCH_QUERY=python

# Telegram для уведомлений
TELEGRAM_TOKEN=your_bot_token
```

### 3. Запуск

```bash
# Один цикл обработки
python main.py

# Непрерывный режим
CONTINUOUS_MODE=true python main.py

# Непрерывный режим с интервалом
python main.py --continuous --limit 5
```

## 🏗️ Архитектура

### Уровень 1: Транспорт и разведка
- **TLS спуфинг**: curl_cffi для подмены JA3/JA4 отпечатков
- **Origin IP bypass**: Обход Cloudflare через прямые запросы к Origin серверу
- **IPv6 ротация**: vproxy с квинтиллионами адресов

### Уровень 2: Логика и анализ
- **NLP фильтрация**: Анализ заказов, извлечение стека, отсев спама
- **RAG-пайплайн**: Сопоставление с портфолио через FAISS
- **Honeypot detection**: Обнаружение генеративных ловушек

### Уровень 3: Взаимодействие
- **Генератор откликов**: Обход AI-детекторов через контроль перплексии/взрывности
- **OPSEC**: Гео/временная консистентность, ISP-туннели

## 📊 Поддерживаемые платформы

| Глобальные | Восточная Европа | СНГ |
|---|---|---|
| Upwork | Just Join IT | Kwork |
| Fiverr | No Fluff Jobs | FL.ru |
| Freelancer | Bulldogjob | Weblancer |
| Toptal | Wild.Codes | Freelance.ru |
| PeoplePerHour | | OneCLancer |
| RemoteOK | | HH.ru |

> ⚠️ **Важно**: Хабр Фриланс (freelance.habr.com) был закрыт и больше не доступен.

## 🔧 Ключевые модули

### TLSClient
```python
from src.evolution import TLSClient

client = TLSClient(browser="chrome_120", proxy="http://127.0.0.1:1080")
response = client.get("https://api.example.com/data")
```

### OriginFinder
```python
from src.evolution import OriginFinder

finder = OriginFinder(shodan_api_key="...")
origins = finder.get_working_origins("example.com")
```

### NLPFilter
```python
from src.brain import NLPFilter

filter = NLPFilter()
analysis = filter.analyze_project(project)
```

### RAGPipeline
```python
from src.brain import RAGPipeline

rag = RAGPipeline(portfolio_path="data/portfolio.json")
match = rag.match_project(project)
```

## 🛡️ OPSEC рекомендации

1. **Не используйте коммерческие VPN** — ASN-сети помечены платформами
2. **Соблюдайте географическую консистентность** — часовой профиль, WebRTC, язык
3. **Ротация IPv6** — vproxy с /64 подсетью (18 квинтиллионов IP)
4. **Временные паттерны** — случайные задержки, рабочее время региона

## 🆕 Новые функции (2025)

### Telegram-подтверждение с inline-кнопками
При отклике на Kwork бот присылает проект + скриншот + отклик с inline-кнопками:
- Цена проекта / +20% / +50% — одним нажатием
- Своя цена — ввод числа в чат
- Пропустить — пропустить проект

### Мультивалютность
Автоматическая конвертация бюджетов из USD, EUR, GBP и др. в рубли для фильтрации по `min_budget`.
Источники курсов: ЦБ РФ (приоритет), exchangerate-api (fallback).

### Уведомления об ответах
Мониторинг входящих сообщений на Kwork и уведомление в Telegram при получении ответа от заказчика.

### Структурированное логирование
Все события (парсинг, фильтрация, отправка, ошибки) сохраняются в SQLite для аналитики.

## 📝 План реализации

- [x] Структура проекта
- [x] Базовый TLS клиент (curl_cffi)
- [x] Реверс-инжиниринг API
- [x] Парсеры Kwork, FL.ru, Upwork, Fiverr
- [x] NLP фильтрация заказов
- [x] RAG пайплайн (FAISS) с инкрементальной переиндексацией
- [x] Генератор откликов с обходом AI-детекторов (Groq + Gemini fallback)
- [x] Мультивалютность (конвертация бюджетов в RUB)
- [x] Rate-limiting для парсеров
- [x] Уведомления об ответах в Telegram (inline-кнопки)
- [x] Структурированное логирование в SQLite
- [x] CI/CD (GitHub Actions)
- [x] Браузерная автоматизация (Nodriver + Camoufox)
- [ ] IPv6 ротация (vproxy настройка) - отложено
- [ ] Гео-консистентность (OPSEC level 3) - отложено

## 📄 Лицензия

MIT

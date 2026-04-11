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

### 3. Запуск

```bash
# Один цикл обработки
python main.py

# Непрерывный режим
CONTINUOUS_MODE=true python main.py
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
| Freelancer | Bulldogjob | Хабр Фриланс |
| Toptal | Wild.Codes | Freelance.ru |

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

## 📝 План реализации

- [x] Структура проекта
- [x] Базовый TLS клиент (curl_cffi)
- [x] Реверс-инжиниринг API
- [x] Парсеры Kwork, FL.ru, Upwork
- [x] NLP фильтрация заказов
- [x] RAG пайплайн (FAISS)
- [x] Генератор откликов с обходом AI-детекторов
- [x] OPSEC модуль
- [ ] IPv6 ротация (vproxy настройка)
- [ ] Браузерная автоматизация (Nodriver/Camoufox)
- [ ] Интеграция с 30+ платформами

## 📄 Лицензия

MIT

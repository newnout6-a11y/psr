# Implementation Plan: Production-Ready Overhaul

## Overview

Комплексная переработка PSR-агента: рефакторинг парсинга Kwork (API + fallback), исправление поисковой стратегии, повышение качества генерации откликов, создание Desktop UI, стабилизация pipeline и удаление мёртвого кода. Реализация на Python 3.12 (backend) и TypeScript/React (desktop UI).

## Tasks

- [ ] 1. Создание KworkService и рефакторинг авторизации
  - [ ] 1.1 Создать модуль `src/platforms/kwork.py` с классом KworkService
    - Реализовать `get_api()` с приоритетом Session Hub → email/password fallback
    - Реализовать retry с задержкой 5с при HTTP 401
    - Ограничить сброс сессии до 3 раз за цикл (`_reset_count`)
    - Реализовать `reset_api()` и `close()`
    - _Requirements: 7.1, 7.2, 7.3, 7.4, 7.5, 7.6, 7.7_

  - [ ]* 1.2 Написать property test для ограничения сбросов сессии
    - **Property 12: Session reset bounded per cycle**
    - **Validates: Requirements 7.7**

  - [ ] 1.3 Рефакторинг `src/parsers/kwork_api_parser.py` для использования KworkService
    - Заменить прямую авторизацию на вызов `KworkService.get_api()`
    - Добавить счётчик ошибок API (3 подряд → fallback на браузер)
    - Реализовать валидацию ответа API (проверка поля `response`)
    - Реализовать fallback на браузерный парсер с логированием WARNING
    - _Requirements: 1.1, 1.3, 1.4, 1.6_

  - [ ]* 1.4 Написать property test для нормализации ProjectItem
    - **Property 1: ProjectItem normalization safety**
    - **Validates: Requirements 1.2, 1.6**

  - [ ]* 1.5 Написать unit tests для KworkService и KworkAPIParser
    - Тест авторизации: Session Hub → email/password → retry → None
    - Тест fallback: API 3 ошибки → браузер → stateData → HTML → пустой список
    - Тест валидации ответа: null, wrong type, missing `response`
    - _Requirements: 1.1–1.7, 7.1–7.7_

- [ ] 2. Рефакторинг SearchStrategy и маппинг категорий
  - [ ] 2.1 Обновить `src/brain/search_strategy.py`
    - Реализовать чтение SEARCH_BRIEF как приоритетного источника
    - Реализовать fallback на SEARCH_QUERY (разделённый "|") → "python"
    - Реализовать `_normalize_query()`: lowercase, ≤50 символов, допустимые символы
    - Реализовать `_merge_queries()`: preferred → learned → AI → исключение negative
    - Реализовать исключение запросов с нулевой конверсией (≥3 запусков)
    - Добавить встроенный fallback-список для kwork
    - _Requirements: 2.1, 2.2, 2.3, 2.4_

  - [ ]* 2.2 Написать property test для нормализации запросов
    - **Property 2: Query normalization constraints**
    - **Validates: Requirements 2.1**

  - [ ]* 2.3 Написать property test для merge ordering
    - **Property 3: Query merge ordering invariant**
    - **Validates: Requirements 2.4**

  - [ ] 2.4 Реализовать маппинг категорий в KworkAPIParser
    - Создать KWORK_CATEGORIES_MAP (словарь ключевых слов → category_ids)
    - Реализовать `_resolve_categories(query)`: union маппинга или [11] по умолчанию
    - Интегрировать маппинг в вызов API при парсинге
    - _Requirements: 2.5, 2.6_

  - [ ]* 2.5 Написать property test для category resolution
    - **Property 4: Category resolution from keywords**
    - **Validates: Requirements 2.5, 2.6**

- [ ] 3. Checkpoint — Убедиться что парсинг и поиск работают
  - Ensure all tests pass, ask the user if questions arise.

- [ ] 4. Улучшение ProposalGenerator
  - [ ] 4.1 Обновить `src/action/proposal_generator.py`
    - Реализовать `_find_best_case(project_skills)`: поиск кейса с максимальным пересечением технологий
    - Обновить промпт для включения кейса (если найден) или генерации без кейса
    - Добавить контекст заказчика в промпт (рейтинг, заказы, % найма) для настройки тона
    - Реализовать `_clean_proposal()`: удаление markdown, эмодзи, цен, вводных фраз, упоминаний профиля
    - Реализовать валидацию: 3-5 предложений, технический подход, завершающий вопрос
    - Реализовать fallback на шаблонный генератор при ошибке LLM
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7_

  - [ ]* 4.2 Написать property test для portfolio case selection
    - **Property 5: Portfolio case selection by maximum intersection**
    - **Validates: Requirements 3.2, 3.3**

  - [ ]* 4.3 Написать property test для очистки текста отклика
    - **Property 6: Proposal text cleaning**
    - **Validates: Requirements 3.5, 3.7**

  - [ ]* 4.4 Написать unit tests для ProposalGenerator
    - Тест генерации с кейсом и без кейса
    - Тест очистки: markdown, эмодзи, цены, вводные фразы
    - Тест fallback на шаблонный генератор
    - _Requirements: 3.1–3.7_

- [ ] 5. Фильтрация, скоринг и дедупликация
  - [ ] 5.1 Обновить `src/filter/project_filter.py`
    - Реализовать keyword-фильтр: стоп-слова из config/filters.yaml (регистронезависимое подстрочное совпадение)
    - Реализовать бюджетный фильтр: min_budget/max_budget, пропуск при null бюджете
    - Реализовать дедупликацию по (platform, project_id), сохраняя первое вхождение
    - _Requirements: 8.1, 8.2, 8.4, 8.6_

  - [ ]* 5.2 Написать property test для stop word filtering
    - **Property 13: Stop word filtering**
    - **Validates: Requirements 8.1**

  - [ ]* 5.3 Написать property test для budget range filtering
    - **Property 14: Budget range filtering**
    - **Validates: Requirements 8.2, 8.6**

  - [ ]* 5.4 Написать property test для score threshold decision
    - **Property 15: Score threshold decision**
    - **Validates: Requirements 8.3**

  - [ ]* 5.5 Написать property test для дедупликации
    - **Property 16: Deduplication by platform and project_id**
    - **Validates: Requirements 8.4**

- [ ] 6. Circuit Breaker и LLM Router
  - [ ] 6.1 Обновить `src/utils/circuit_breaker.py`
    - Реализовать state machine: CLOSED → OPEN (после 3 consecutive failures) → HALF_OPEN (после recovery time) → CLOSED (при success)
    - В HALF_OPEN разрешить ровно один вызов
    - При success в HALF_OPEN сбросить все счётчики и перейти в CLOSED
    - Логировать переходы состояний и длительность паузы
    - _Requirements: 5.3, 5.7_

  - [ ]* 6.2 Написать property test для Circuit Breaker state machine
    - **Property 10: Circuit breaker state machine correctness**
    - **Validates: Requirements 5.3**

  - [ ] 6.3 Обновить `src/brain/llm_router.py`
    - Убедиться что task-specific маршрутизация работает через env-переменные
    - Убедиться что fallback по health-score корректен
    - Убедиться что get_provider_health() возвращает полную статистику
    - _Requirements: 10.1, 10.2, 10.3, 10.4, 10.5_

  - [ ]* 6.4 Написать property test для LLM Router provider ordering
    - **Property 18: LLM Router provider ordering by health-score**
    - **Validates: Requirements 10.2, 10.4**

- [ ] 7. Checkpoint — Убедиться что core pipeline работает
  - Ensure all tests pass, ask the user if questions arise.

- [ ] 8. Orchestrator: стабильность и наблюдаемость
  - [ ] 8.1 Обновить `src/orchestrator.py`
    - Добавить per-element try/except на каждом этапе: при ошибке → status="error", продолжить остальные
    - Добавить логирование каждого этапа: вход/выход количество, время в мс
    - Реализовать возврат словаря статистики с ключами: parsed, active, filtered, ai_passed, vetted, queued, auto_ready, sent
    - Реализовать запись cycle_logs в logs.db
    - Обработать случай полной недоступности парсеров: вернуть нулевую статистику без аварийного завершения
    - _Requirements: 5.1, 5.2, 5.4, 5.5, 5.6_

  - [ ]* 8.2 Написать property test для pipeline fault isolation
    - **Property 9: Pipeline fault isolation**
    - **Validates: Requirements 5.2**

  - [ ]* 8.3 Написать property test для cycle statistics structure
    - **Property 11: Cycle statistics structure invariant**
    - **Validates: Requirements 5.5**

  - [ ]* 8.4 Написать property test для dry run mode
    - **Property 17: Dry run mode invariant**
    - **Validates: Requirements 9.6**

- [ ] 9. Runtime env overrides и валидация настроек
  - [ ] 9.1 Реализовать `_apply_runtime_env()` и `_restore_runtime_env()` в API layer
    - Сохранять предыдущие значения env-переменных перед override
    - Восстанавливать оригинальные значения (или unset) после завершения цикла
    - _Requirements: 4.6_

  - [ ]* 9.2 Написать property test для env override/restore round-trip
    - **Property 8: Runtime env override/restore round-trip**
    - **Validates: Requirements 4.6**

  - [ ] 9.3 Реализовать валидацию настроек в API
    - Валидация диапазонов: QUERY_COUNT [1,20], PAGES_TO_PARSE [1,10], TOP_PROJECTS [0,50], AI_SCORE_THRESHOLD [1,10], limit [1,20]
    - Валидация SEARCH_BRIEF: до 500 символов
    - Валидация редактирования: текст [1,2000], цена [min_budget, max_budget]
    - _Requirements: 4.5, 9.3, 9.4_

  - [ ]* 9.4 Написать property test для settings validation
    - **Property 7: Settings validation correctness**
    - **Validates: Requirements 4.5**

  - [ ]* 9.5 Написать property test для edit validation
    - **Property 19: Edit validation constraints**
    - **Validates: Requirements 9.3**

- [ ] 10. Checkpoint — Убедиться что backend стабилен
  - Ensure all tests pass, ask the user if questions arise.

- [ ] 11. FastAPI endpoints и WebSocket
  - [ ] 11.1 Обновить/создать API endpoints в `src/api/routes/`
    - POST /api/orchestrator/start — запуск цикла с runtime env overrides
    - POST /api/orchestrator/stop — остановка цикла
    - GET /api/orchestrator/status — текущий статус (idle/running/error + этап + счётчики)
    - GET /api/candidates — список с пагинацией (≤30 на страницу)
    - POST /api/candidates/{id}/approve — подтверждение отклика
    - POST /api/candidates/{id}/skip — пропуск
    - PATCH /api/candidates/{id} — редактирование текста/цены с валидацией
    - GET /api/settings — текущие настройки
    - PUT /api/settings — обновление runtime-настроек
    - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.5, 9.1, 9.2, 9.3, 9.4, 9.5_

  - [ ] 11.2 Реализовать WebSocket endpoint для real-time логов
    - WS /ws — трансляция логов и статуса pipeline
    - Задержка отображения ≤1с от момента записи
    - _Requirements: 4.7_

  - [ ]* 11.3 Написать unit tests для API endpoints
    - Тест запуска/остановки цикла
    - Тест пагинации кандидатов
    - Тест approve/skip/edit
    - Тест валидации настроек
    - _Requirements: 4.1–4.8, 9.1–9.5_

- [ ] 12. Desktop UI — Dashboard и Pipeline Status
  - [ ] 12.1 Создать компонент Dashboard в `desktop/src/pages/Dashboard.tsx`
    - Отображение статуса pipeline: idle/running/error
    - При running: текущий этап и счётчик обработанных/всего
    - Кнопка запуска цикла с параметрами из настроек
    - Отображение ошибки при неудачном запуске (сохранение параметров)
    - Индикатор WebSocket-соединения
    - _Requirements: 4.1, 4.2, 4.3, 4.8_

  - [ ] 12.2 Создать компонент Candidates в `desktop/src/pages/Candidates.tsx`
    - Список кандидатов с пагинацией (≤30 на страницу)
    - Для каждого: статус, AI-скор, текст отклика
    - Действия: approve, skip, редактирование текста и цены
    - _Requirements: 4.4_

  - [ ] 12.3 Создать компонент Settings в `desktop/src/pages/Settings.tsx`
    - Поля: SEARCH_BRIEF, QUERY_COUNT, PAGES_TO_PARSE, TOP_PROJECTS, AI_SCORE_THRESHOLD, лимит отправок
    - Валидация диапазонов на клиенте перед отправкой
    - Скрытие секций отключённых платформ и OSINT
    - _Requirements: 4.5, 6.5, 6.6_

  - [ ] 12.4 Создать компонент Logs в `desktop/src/pages/Logs.tsx`
    - Real-time отображение логов через WebSocket
    - Автоматическое переподключение каждые 5с (до 10 попыток)
    - Индикатор потери связи
    - _Requirements: 4.7, 4.8_

- [ ] 13. Упрощение архитектуры и удаление мёртвого кода
  - [ ] 13.1 Обновить инициализацию Orchestrator
    - При PLATFORMS=kwork: инициализировать только KworkAPIParser
    - При OSINT_ENABLED=false или не задан: не создавать OSINTAggregator
    - При пустом OSINT_PROBIV_PROVIDERS: не импортировать probiv-модули
    - _Requirements: 6.1, 6.2, 6.4_

  - [ ] 13.2 Обновить Desktop UI для скрытия неактивных секций
    - Скрывать настройки freelance_ru/hh_ru если не в PLATFORMS
    - Скрывать настройки OSINT если OSINT_ENABLED=false
    - _Requirements: 6.5, 6.6_

- [ ] 14. Отправка откликов с подтверждением
  - [ ] 14.1 Обновить flow подтверждения в Orchestrator
    - При semi_auto + auto_ready: уведомление в Desktop UI → fallback Telegram → pending_review
    - При approve: отправка через браузерный flow с таймаутом 60с
    - При edit: валидация текста [1,2000] и цены [min_budget, max_budget]
    - При ошибке валидации: отклонить, сообщение об ошибке, сохранить pending_review
    - При ошибке отправки: status=error, уведомить пользователя
    - При dry_run: все кандидаты → draft, без реальной отправки
    - _Requirements: 9.1, 9.2, 9.3, 9.4, 9.5, 9.6_

- [ ] 15. Интеграция и wiring
  - [ ] 15.1 Интегрировать все компоненты в единый pipeline
    - Подключить KworkService → KworkAPIParser → Orchestrator
    - Подключить обновлённый SearchStrategy
    - Подключить обновлённый ProposalGenerator
    - Подключить Desktop UI → FastAPI → Orchestrator
    - Убедиться что полный цикл (парсинг → отправка) работает end-to-end
    - _Requirements: 6.3_

  - [ ]* 15.2 Написать integration tests
    - Тест полного цикла с mock API
    - Тест WebSocket real-time логов
    - Тест approve/skip flow через API
    - _Requirements: 6.3, 4.7, 9.1_

- [ ] 16. Final checkpoint — Убедиться что все тесты проходят
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- Each task references specific requirements for traceability
- Checkpoints ensure incremental validation
- Property tests validate universal correctness properties from the design document (19 properties total)
- Unit tests validate specific examples and edge cases
- Python 3.12 async-first backend, TypeScript/React desktop UI
- Hypothesis library used for property-based testing
- All property tests tagged with `# Feature: production-ready-overhaul, Property {N}: {title}`

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1", "2.1", "6.1", "6.3"] },
    { "id": 1, "tasks": ["1.2", "1.3", "2.2", "2.3", "2.4", "6.2", "6.4"] },
    { "id": 2, "tasks": ["1.4", "1.5", "2.5", "4.1", "5.1"] },
    { "id": 3, "tasks": ["4.2", "4.3", "4.4", "5.2", "5.3", "5.4", "5.5"] },
    { "id": 4, "tasks": ["8.1", "9.1", "9.3"] },
    { "id": 5, "tasks": ["8.2", "8.3", "8.4", "9.2", "9.4", "9.5"] },
    { "id": 6, "tasks": ["11.1", "11.2", "13.1", "14.1"] },
    { "id": 7, "tasks": ["11.3", "12.1", "12.2", "12.3", "12.4", "13.2"] },
    { "id": 8, "tasks": ["15.1"] },
    { "id": 9, "tasks": ["15.2"] }
  ]
}
```

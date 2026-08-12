# Kwork Buyer Search: полный план рефакторинга и реализации

Дата: 2026-07-16  
Статус: готово к декомпозиции на implementation PR  
Область: PSR repository; код VPNTEN не изменяется  
Связанный журнал: docs/kwork_buyer_search_refactor_journal_20260716.md

## 0. Итоговое архитектурное решение

Поиск покупателей должен стать отдельным продуктовым контуром Buyer Search, а не дополнительной кнопкой внутри существующего seller-market экрана и не расширением синхронного get_buyer_scout().

Целевой конвейер:

~~~mermaid
flowchart LR
    A["Рубрика, brief или ручные запросы"] --> B["AI Query Planner"]
    B --> C["Уникальный durable query plan"]
    C --> D["30 account workers"]
    D --> E["30 уникальных healthy egress IP"]
    E --> F["Read-only discovery"]
    F --> G["Canonical project DB и observations"]
    G --> H["Selective enrichment"]
    H --> I["Фильтры, scoring, shortlist"]
    I --> J["JSONL, Markdown, CSV или ZIP export"]
    I --> K["Attachment-aware proposal draft"]
    K --> L["Preflight и идемпотентная отправка"]
    L --> M["Account-bound conversation"]
    M --> N["AI copilot для переписки"]
~~~

Основные решения:

1. Переиспользовать durable runtime из src/platforms/kwork_supply: jobs, workers, operations, attempts, commands, leases, account pool, transports, WebSocket events и recovery.
2. Не переиспользовать seller-specific mapper, listing schema и analyzer как buyer domain.
3. Добавить job_kind/workflow dispatch и отдельный buyer-domain со своей схемой данных.
4. Разделить discovery и enrichment. Первая волна должна быстро получить максимальное число уникальных заказов; дорогие details, buyer history и attachments загружаются отдельно.
5. Поиск остаётся строго read-only. Отправка отклика является отдельной явной state machine.
6. Один worker всегда означает один конкретный Kwork account, отдельный token/cookie jar/persona и один уникальный текущий egress IP.
7. Signup/verified IP является приоритетом, а не жёсткой привязкой. Занятый приоритетный IP никогда не должен блокировать аккаунт при наличии другого свободного healthy IP.
8. SQLite можно оставить для первой версии с 30 in-process workers только после исправления polling, heartbeat и lease fencing и после прохождения нагрузочного gate.
9. Отклик и чат должны быть привязаны к sender account. Глобальная Kwork session для этих операций недопустима.
10. gpt-5.6-sol должен задаваться как task-specific strongest-model alias для proposal/chat, а не быть жёстко прошитым в общий LLM router.

## 1. Продуктовая цель

Оператор должен уметь:

- создать новый buyer-search run из brief, рубрики или ручного набора запросов;
- увидеть найденную Kwork taxonomy, реальные category IDs и доступные attributes;
- попросить AI сгенерировать большой уникальный набор поисковых запросов;
- проверить, отредактировать, отключить и перераспределить запросы до запуска;
- запустить до 30 account workers одновременно;
- видеть account, slot, current IP, query, page, latency, retry и error каждого worker;
- собирать все найденные заказы в durable DB без потери provenance;
- видеть views, offers, budget, age, buyer history, attachments и matched queries;
- фильтровать, сортировать, тегировать и сохранять подборки;
- скачать полноценный dataset с описаниями и метаданными;
- открыть выбранный заказ и распарсить все доступные attachments;
- сгенерировать несколько вариантов отклика сильной моделью;
- проверить price, delivery, account, connects и remote state перед отправкой;
- отправить ровно один отклик и получить локальный/remote audit;
- продолжить диалог в приложении через тот же account;
- использовать AI для draft, rewrite, summary и next action без автоматической отправки.

## 2. Неподлежащие компромиссу инварианты

### 2.1 Account identity

- Один активный worker владеет ровно одним account lease.
- Один account не может одновременно принадлежать двум workers.
- Mobile token, web cookies, headers, persona и proxy создаются из account context конкретного worker.
- Нельзя использовать общий Session Hub cookie set для параллельных workers.
- Discovery account записывается в observation provenance.
- Sender account записывается отдельно и после отправки закрепляется за conversation.

### 2.2 IP и VPNTEN

Порядок выбора route:

1. Сохранённый route текущего worker, если он healthy, свободен и его egress IP уникален.
2. Свободный route с signup/verified IP аккаунта.
3. Свободный preferred slot аккаунта.
4. Любой другой свободный healthy route с уникальным egress IP.

Жёсткие правила:

- signup/verified IP только увеличивает приоритет;
- занятый другим worker signup IP пропускается;
- account не переходит в blocked, если существует другой свободный уникальный route;
- одновременно работающие workers не могут иметь одинаковый egress IP;
- transport без подтверждённого egress IP не входит в effective capacity;
- direct fallback запрещён;
- rotation выполняется после drain request и инвалидации account-scoped client;
- смена route generation немедленно инвалидирует cached IP и HTTP client;
- сохранённый affinity не важнее эксклюзивности и работоспособности.

Текущий MarketIdentityPool._route_candidates() уже реализует правильную основу:

- src/platforms/kwork_supply/identity_pool.py:541;
- исключает occupied transport и occupied IP;
- сортирует retained, preferred_ip, preferred_slot, fallback.

Это поведение нельзя сломать при обобщении runtime. Оно должно быть закреплено integration-тестом Buyer Search.

### 2.3 Read-only discovery

Поисковый worker имеет право:

- читать taxonomy;
- читать projects list;
- читать project/want detail;
- читать buyer history;
- читать suggestions;
- скачивать входящие attachments.

Поисковый worker не имеет права:

- создавать или редактировать offer;
- вызывать addview;
- скрывать want;
- сохранять review;
- restart/manage project;
- писать в inbox;
- выполнять browser action, меняющий remote state.

Mutation endpoints должны быть физически недоступны discovery handler через capability-scoped client.

### 2.4 Дедупликация

- Query uniqueness гарантируется центральным planner и DB constraint, а не доброй волей worker.
- Request uniqueness учитывает run, category, normalized query, filters и page/cursor.
- Project canonical uniqueness: platform плюс remote_project_id.
- Один project, найденный десятью запросами, создаёт одну canonical row и десять immutable observations/matches.
- Повторный page fetch после crash должен быть идемпотентным.
- Proposal send должен иметь отдельную idempotency/reconciliation защиту.

### 2.5 Прозрачность

Любое число в UI должно происходить из durable state:

- effective worker capacity;
- active account leases;
- unique verified egress count;
- generated/approved/assigned/exhausted queries;
- requests, pages, unique projects, duplicates;
- 403, 429, timeout, quarantine, rotation;
- details/attachments/scoring progress;
- shortlist/export/proposal/chat state.

Имитация progress по таймеру и хранение основного dataset в localStorage запрещены.

## 3. Что уже существует в repository

### 3.1 Durable market control plane

Готовые для переиспользования части:

- src/platforms/kwork_supply/models.py:20: lifecycle models;
- src/platforms/kwork_supply/repository.py:83: SQLite durable repository;
- src/platforms/kwork_supply/coordinator.py:48: job coordination;
- src/platforms/kwork_supply/worker.py:42: generic worker actor с handler map;
- src/platforms/kwork_supply/supervisor.py:36: worker fleet supervisor;
- src/platforms/kwork_supply/identity_pool.py:77: account/transport identity pool;
- src/platforms/kwork_supply/executor.py:1728: account-scoped KworkMarketClient factory;
- src/api/routes/kwork_market_jobs.py:487: REST control plane;
- src/api/routes/kwork_market_jobs.py:1151: sequenced WebSocket stream;
- desktop/src/features/kwork-market/: существующий typed UI control plane.

В текущей версии executor создаёт отдельный Kwork client из account email/password/cookies/persona headers и worker proxy. Более старые документы о единственном общем Session Hub не должны использоваться как описание текущего executor.

### 3.2 Legacy buyer scout

Текущий buyer scout:

- request model: src/api/routes/kwork.py:248;
- inline route: src/api/routes/kwork.py:1151;
- основная логика: src/platforms/kwork_market.py:3326;
- UI: desktop/src/pages/KworkMarket.tsx.

Проблемы:

- один длинный HTTP request;
- один client/account;
- запросы, страницы, detail, want и buyer history выполняются последовательно;
- нет durable run;
- нет query leases;
- нет 30-worker assignment;
- нет полной DB и server-side filters;
- нет resumability;
- compact result хранится в localStorage;
- UI показывает только малую верхнюю часть результата.

Legacy endpoint следует оставить временно как compatibility route, но не развивать.

### 3.3 Proposal и conversations

Существуют:

- ProposalDB;
- proposal generation;
- ограниченный attachment text extraction;
- proposal sender;
- conversation sync;
- AI reply draft;
- message send routes.

Критические пробелы:

- ProposalDB теряет multi-query provenance;
- conversations не включают account_registration_id;
- sender использует глобальную session;
- sender отбрасывает attachments;
- remote success может произойти до local commit;
- blind retry состояния sending способен создать duplicate;
- chat sync watermark не разделён по accounts;
- reply_linker вызывает отсутствующие методы ProposalDB;
- legacy phase-3 smoke не проходит из-за отсутствующего replied_at и связанных migration gaps.

### 3.4 Фактическая локальная capacity на 2026-07-16

Read-only snapshot data/kwork_market_jobs.db:

| Метрика | Значение |
|---|---:|
| Jobs | 13 |
| Account inventory | 41 |
| Market-enabled accounts | 40 |
| Historical identity bindings | 154 |
| Transport rows | 121 |
| Workers | 494 |
| Operations | 11 469 |
| Listings | 13 824 |
| Healthy transport rows | 101 |
| Healthy с непустым verified egress | 12 |
| Уникальные verified egress IP | 10 |

Вывод: 40 accounts достаточно для account pool, но текущая подтверждённая одновременная IP capacity равна 10, а не 30. Запуск 30 workers нельзя считать готовым, пока VPNTEN/health verification не дают минимум 30 уникальных healthy egress IP одновременно.

## 4. Карта Kwork buyer API

### 4.1 Уровни доказанности

- A: повторно подтверждено live probes и сохранёнными ответами.
- B: подтверждено единично или с неполной семантикой.
- C: найдено в JS или установленной библиотеке, но нет сохранённого успешного live mutation.
- D: контракт не восстановлен.

### 4.2 Discovery endpoints

| Endpoint | Auth | Назначение | Ключевые поля | Надёжность |
|---|---|---|---|---|
| POST /projects | mobile Basic плюс token | Быстрый список заказов | response, paging, total, connects | A |
| POST /getWantsCount | mobile Basic плюс token | Counts/facets | count, filters | A |
| POST /project | mobile Basic плюс token | Card-like detail | project card fields | A |
| POST /want | mobile Basic плюс token | Rich detail | views, orders, views_history, dates | A |
| GET /projects | web cookies | Web stateData | full description, files, views_dirty, kwork_count | A |
| GET /projects/list/{username} | web cookies | Buyer project history | wants history | A |
| GET /projects/{id}/view | web cookies | HTML detail | detail page | B |
| POST /want-search/suggest | web/session form | Query suggestions | suggestions | A |

### 4.3 POST /projects

Подтверждённые request fields:

- categories;
- page;
- query;
- price_from;
- price_to;
- hiring_from;
- kworks_filter_from;
- kworks_filter_to.

Основные response fields:

- id;
- status;
- user_id;
- username;
- title;
- description;
- price;
- possible_price_limit;
- allow_higher_price;
- offers;
- time_left;
- parent_category_id;
- category_id;
- user_projects_count;
- user_active_projects_count;
- user_hired_percent;
- is_viewed;
- has_offer;
- already_work;
- user_need_kwork;
- user_need_portfolio;
- paging.page;
- paging.total;
- paging.limit;
- paging.pages;
- connects.

Обычно возвращается 12 records/page.

Скоростное правило: первая discovery волна не вызывает /getWantsCount перед /projects. /projects уже возвращает paging.total. Дополнительный count request допустим только для UI facets/forecast или planner analytics.

### 4.4 POST /want

Rich detail добавляет:

- views;
- orders;
- offers;
- views_history;
- date_create;
- date_active;
- date_expire;
- want_status_id;
- price_limit.

/want не должен вызываться для каждого найденного project в критическом discovery path. Он является отдельной enrichment operation.

### 4.5 Authenticated web /projects stateData

Особенно ценные fields:

- pagination.data[].files;
- views_dirty;
- kwork_count;
- полный description;
- priceLimit;
- possiblePriceLimit;
- category_id;
- buyer/user data;
- availableDurations;
- isHigherPrice;
- isNeural;
- date_create/date_active/date_expire;
- isWantActive/isWantArchive;
- filters.by_kworks;
- filters.by_budget;
- counts;
- attributesCount;
- favouriteCategories.

Необходимо хранить web raw snapshot отдельно от mobile raw snapshot. views_dirty нельзя объявлять эквивалентом /want.views до controlled comparison на одном project ID.

### 4.6 Фильтры

Подтверждённый mobile-to-web mapping:

| Mobile | Web |
|---|---|
| query | keyword |
| categories | c |
| price_from | price-from |
| price_to | price-to |
| hiring_from | hiring-from |
| kworks_filter_to=5 | kworks-filters=0 |
| budget buckets | prices-filters |

Важно:

- keyword, kworks_filters и prices_filters нельзя отправлять в mobile API;
- sort=date пока не доказан;
- classifier/attribute server filter для buyer API не доказан;
- classifier/attributes в первой версии используются AI planner и local post-filter, но UI не называет их remote server filters.

### 4.7 Taxonomy

Подтверждённые root rubrics:

| ID | Рубрика |
|---:|---|
| 15 | Design |
| 11 | Development and IT |
| 5 | Texts and translations |
| 17 | SEO and traffic |
| 45 | Social media and marketing |
| 7 | Audio/video |
| 83 | Business and life |

Endpoints:

- POST /catalogRubrics;
- POST /catalogCategories с camelCase rubricId;
- POST /categories;
- POST /catalogMainv2;
- POST /categoryAttributes с category_id;
- POST /catalogFilters с camelCase categoryId;
- POST /category с category_id.

Примеры Development/IT:

- 38: site repair/configuration;
- 37: website creation;
- 41: scripts, bots and mini apps;
- 79: frontend;
- 80: desktop programming.

### 4.8 Proposal mutation chain

Предполагаемая последовательность:

1. GET /new_offer?project={id};
2. POST /quick-faq/init;
3. POST /wants/create_offer_draft;
4. POST /projects/check_is_template;
5. POST /api/offer/createoffer.

Основные fields:

- wantId;
- offerType;
- description;
- kwork_duration;
- kwork_price;
- kwork_name;
- csrftoken;
- draftKey.

Edit path:

- POST /api/offer/editoffer.

Preflight:

- GET /wants/{id}/check_offer_notify;
- POST /projects/check_is_template;
- POST /wants/portfolio.

Это нельзя считать production-ready mutation contract до controlled live capture успешного createoffer.

### 4.9 Attachments и chat

Известно:

- web project list содержит files[];
- static JS показывает POST /api/file/upload;
- library содержит /wants/upload_offer_file;
- mobile chat primitives: inboxCreate, inboxRead, markInboxTracksAsRead;
- web read: GET /inbox и GET /inbox/{username};
- static send: POST /sendmessage;
- notification: GET /api/user/checknotify.

Неизвестно до live verification:

- полная schema непустого project files[];
- canonical authenticated download URL;
- chat attachment request/response;
- точный inbox pagination contract;
- полный успешный createoffer response;
- remote offer receipt/idempotency semantics.

### 4.10 Read-only denylist

Discovery client запрещает:

- POST /api/offer/addview;
- POST /wants/hide_want;
- POST /wants/review/save;
- POST /projects/manage/restart;
- POST /api/offer/createoffer;
- POST /api/offer/editoffer;
- POST /wants/create_offer_draft;
- POST /sendmessage;
- любые browser-side actions с неясной мутацией.

## 5. Целевая архитектура

### 5.1 Bounded contexts

~~~mermaid
flowchart TB
    UI["Buyer Search Desktop Feature"]
    API["Buyer Search REST and WS"]
    RUNTIME["Durable Market Runtime"]
    BUYER["Buyer Domain"]
    KWORK["Account-scoped Kwork Sources"]
    ID["Identity Pool"]
    VPN["VPNTEN Adapter"]
    LOCAL["Local Processing Pools"]
    OUT["Outreach Domain"]
    CHAT["Conversation Domain"]

    UI --> API
    API --> BUYER
    BUYER --> RUNTIME
    RUNTIME --> ID
    ID --> VPN
    RUNTIME --> KWORK
    BUYER --> LOCAL
    BUYER --> OUT
    OUT --> CHAT
~~~

Контуры:

- Durable Runtime: generic jobs/workers/operations/leases/events.
- Buyer Search: runs, queries, tasks, projects, observations, details, scoring.
- Attachment Processing: download, extract, OCR, vision, derivatives.
- Outreach: drafts, preflight, send intents, attempts, receipts, reconciliation.
- Conversations: account-bound dialogs/messages/sync cursors/AI drafts.
- Desktop Feature: отдельные routes, tables, inspectors и command views.

### 5.2 Стратегия обобщения kwork_supply

Первая миграция не должна массово переименовывать market_* tables и ломать seller flow.

Добавить:

- market_jobs.job_kind;
- market_jobs.config_json;
- market_jobs.target_unique_items;
- operation handler registry по job_kind;
- buyer operation kinds;
- generic result commit interface;
- lease fencing fields.

Обернуть существующую supply-логику в SupplyWorkflow, а новую логику реализовать в BuyerDiscoveryWorkflow.

Не переносить buyer projects в market_supply_listings. Это разные сущности и разные метрики.

### 5.3 Operation kinds

Добавить:

- PLAN_BUYER_QUERIES;
- FETCH_BUYER_PROJECTS;
- FETCH_BUYER_PROJECT_DETAIL;
- FETCH_BUYER_PROFILE_HISTORY;
- FETCH_BUYER_PROJECT_WEB_STATE;
- DOWNLOAD_BUYER_ATTACHMENT;
- PARSE_BUYER_ATTACHMENT;
- SCORE_BUYER_PROJECT;
- BUILD_BUYER_EXPORT;
- RECONCILE_BUYER_PROJECT;
- SYNC_BUYER_CONVERSATION.

Proposal send не должен быть обычной discovery operation. Он использует отдельную outreach state machine с жёсткой идемпотентностью.

## 6. Режимы запуска

### 6.1 Brief mode

Пользователь вводит:

- что он продаёт;
- примеры целевых заказов;
- языки;
- бюджеты;
- нежелательные типы работ;
- desired project count;
- max workers;
- queries per worker;
- enrichment policy.

AI:

- предлагает рубрики/categories;
- показывает ID и причину выбора;
- генерирует query universe;
- оценивает overlap;
- формирует worker bundles.

### 6.2 Category mode

Пользователь выбирает taxonomy node.

Система:

- получает category metadata;
- загружает categoryAttributes/catalogFilters;
- показывает доступный пласт категорий;
- строит vocabulary из category name, children, attributes, catalogMainv2 и suggest;
- генерирует уникальные запросы без ручного keyword.

### 6.3 Manual mode

Пользователь вставляет список запросов.

Система:

- нормализует;
- отмечает exact duplicates;
- отмечает semantic collisions;
- предлагает expansion;
- требует category scope или осознанный all-categories режим.

### 6.4 Hybrid mode

Пользователь задаёт seed queries и category.

AI расширяет seeds, но сохраняет origin и parent query для аудита.

## 7. Query Planner

### 7.1 Центральный planner

Worker не генерирует query самостоятельно. Planner создаёт и утверждает общий durable план до distribution.

Inputs:

- brief;
- rubric/category IDs;
- taxonomy subtree;
- category attributes;
- Kwork suggest;
- historical query performance;
- negative keywords;
- budget/hiring/offer filters;
- number of workers;
- queries per worker;
- target unique projects.

Output на каждый query:

- query_id;
- text;
- normalized_text;
- origin;
- parent_query_id;
- rationale;
- priority;
- category_id;
- category_path;
- filter_json;
- filter_hash;
- semantic_fingerprint;
- predicted breadth;
- predicted competition;
- enabled;
- approval state.

### 7.2 Нормализация

Минимум:

- Unicode normalization;
- lowercase;
- исправление сохранённого mojibake;
- trim/collapse whitespace;
- punctuation normalization;
- ё/е policy;
- URL/emoji removal для hash;
- stem/lemma representation для similarity;
- canonical filter JSON.

Exact constraint:

UNIQUE(run_id, category_id, normalized_text, filter_hash)

Request constraint:

UNIQUE(run_id, request_fingerprint)

request_fingerprint включает query, category, filters, page/cursor и source.

### 7.3 Semantic diversity

Planner:

1. Перегенерирует больше кандидатов, чем нужно.
2. Вычисляет embeddings/fingerprints.
3. Кластеризует близкие формулировки.
4. Сохраняет лучший query из каждого кластера.
5. Даёт UI collision report.
6. Позволяет вручную оставить похожие запросы, если они имеют разный intent.

Пример разных intents для telegram bot:

- telegram bot;
- автоматизация telegram;
- telegram mini app;
- интеграция telegram с CRM;
- telegram payment bot;
- парсер telegram;
- поддержка существующего telegram bot;
- перенос bot на другой hosting.

### 7.4 Distribution

Planner создаёт query tasks и bundles.

Правила:

- исходно каждому worker выдаётся уникальная пачка;
- query ownership не вечный: после worker failure task возвращается в queue;
- work stealing разрешён;
- один request task leased только одному worker;
- следующий page создаётся после анализа paging;
- нет общей barrier между всеми workers;
- high-value query может иметь несколько pages, но page fingerprints уникальны;
- глобальный project dedupe не зависит от query assignment.

## 8. Исполнение 30 workers

### 8.1 Preflight capacity

Перед запуском рассчитываются:

- requested_workers;
- eligible_accounts;
- healthy_transports;
- transports_with_fresh_verified_egress;
- unique_egress_ips;
- collision_count;
- quarantined transports;
- accounts_without_any_route;
- effective_capacity.

Формула:

effective_capacity = min(
    requested_workers,
    eligible_unique_accounts,
    healthy_unique_egress_ips
)

Run не должен ложно показывать 30/30, если verified unique capacity равна 10.

Политика запуска:

- strict mode: run ждёт requested capacity;
- elastic mode: стартует effective capacity и поднимает workers по мере появления routes;
- recommended default: elastic с явным баннером 10 из 30 и причиной.

### 8.2 Identity allocation

Псевдологика:

~~~text
for account in eligible_accounts:
    candidates = healthy_routes
        excluding occupied_transport_ids
        excluding occupied_egress_ips
        excluding stale_or_unverified_routes

    rank candidates:
        0 retained route
        1 signup/verified IP
        2 preferred slot
        3 any fallback route

    lease first candidate atomically

    if signup IP is occupied:
        continue to preferred slot or fallback

    block only if no unique healthy route exists
~~~

UI должен показывать route_mode:

- retained;
- preferred_ip;
- preferred_slot;
- fallback.

Fallback не является ошибкой. Это нормальный режим, если приоритетный IP занят.

### 8.3 Одновременность

Network pool:

- максимум один in-flight Kwork request на worker;
- до 30 одновременных requests;
- отдельные account clients;
- отдельные proxies/IP;
- per-endpoint timeout;
- per-account и global token buckets;
- adaptive concurrency снижает реальный effective limit, а не только пишет recommendation.

Local pools:

- LLM query planning;
- text parsing;
- OCR/vision;
- scoring;
- export compression.

Local processing не удерживает scarce VPNTE identity lease, если удалённый download уже завершён.

### 8.4 Fast discovery

Первый request worker:

POST /projects с assigned query/category/filters/page=1.

После ответа:

- сохранить raw artifact;
- нормализовать все cards;
- commit одной batch transaction;
- создать observations/matches;
- прочитать paging;
- создать next page task только при необходимости;
- обновить query counters.

Не делать перед ним:

- /getWantsCount;
- /want для каждой card;
- buyer history;
- file download;
- LLM scoring.

### 8.5 Selective enrichment

Policies:

- none;
- top N per query;
- top N per run;
- shortlisted only;
- all, ручное подтверждение;
- adaptive by preliminary score.

Enrichment источники:

- mobile /want;
- mobile /project;
- web /projects state;
- buyer project history;
- attachment manifest/download.

### 8.6 403, 429 и timeout

403:

- classify challenge vs route failure;
- quarantine account/route/endpoint tuple;
- сохранить evidence;
- drain worker;
- rotate/rebind после policy;
- не повторять бесконечно тот же request.

429:

- учитывать Retry-After;
- снижать source/account concurrency;
- reschedule task;
- не ротировать IP автоматически без policy.

Timeout:

- unknown read outcome безопасно retry по request fingerprint;
- записать latency и route generation;
- после threshold проверить route health;
- не смешивать network failure с empty result.

## 9. Исправления durable runtime до Buyer Search

### 9.1 Lease fencing

Текущий commit_accepted_batch проверяет job/shard/op, но не гарантирует, что commit выполняет текущий lease owner/attempt.

Добавить:

- market_operations.lease_fence INTEGER NOT NULL DEFAULT 0;
- market_operations.leased_attempt_id;
- lease operation атомарно увеличивает lease_fence;
- handler получает lease_fence;
- commit принимает worker_id, attempt_id, lease_fence;
- transaction проверяет owner, non-expired deadline, attempt и fence;
- stale commit возвращает LeaseLost и ничего не пишет.

Тест:

1. Worker A получает fence 7.
2. Lease истекает.
3. Worker B получает fence 8.
4. A пытается commit с fence 7.
5. Ни одна buyer row/observation не добавляется.

### 9.2 Identity heartbeat

Текущий operation heartbeat продлевает operation lease, но identity lease обновляется только через persist state. Длинный handler способен пережить identity TTL.

Добавить единый heartbeat task:

- renew operation lease;
- renew identity lease;
- update worker heartbeat не чаще 5-10 секунд;
- cancel handler при потере любого lease;
- release identity в finally.

### 9.3 Убрать 250 ms write polling

Текущий idle loop способен создавать сотни SQLite write transactions/sec на 30 workers.

Изменения:

- worker ждёт asyncio Condition/Event или repository notification;
- fallback poll 2-5 секунд только для recovery;
- LEASING/IDLE state не пишется каждый tick;
- heartbeat отделён от visual state;
- supervisor будит workers после enqueue/resume/retry deadline;
- due-task scheduler будит по ближайшему not_before;
- state persistence coalesce.

### 9.4 SQLite writer policy

Для первой версии:

- WAL;
- busy_timeout;
- один writer actor/queue;
- batch UPSERT;
- одна transaction на 12-card page;
- bounded queue;
- writer queue depth metric;
- transaction duration metric;
- checkpoint policy;
- короткие read connections;
- отсутствие synchronous network call внутри transaction.

Gate для 30 workers:

- нет SQLITE_BUSY в steady run;
- writer queue p95 меньше 100 ms;
- commit p95 меньше 50 ms для 12 cards;
- event lag p95 меньше 500 ms;
- heartbeat misses равны 0;
- no stale commits.

Если gate не проходит после оптимизации, вводится Postgres repository adapter. Не нужно начинать миграцию на Postgres до измерения.

### 9.5 Async transport refresh

Синхронный VPNTEN refresh нельзя выполнять внутри event loop.

Решение:

- asyncio.to_thread или async provider;
- timeout/circuit breaker;
- cached snapshot с explicit freshness;
- stale egress не считается verified capacity;
- route generation change invalidates cached client.

## 10. Buyer data model

### 10.1 buyer_runs

Поля:

- run_id;
- job_id FK;
- name;
- mode;
- brief;
- category_scope_json;
- filter_json;
- requested_workers;
- query_batch_size;
- target_unique_projects;
- enrichment_policy;
- scoring_profile_id;
- state;
- created_by;
- created_at;
- started_at;
- completed_at;
- stopped_at;
- config_version.

### 10.2 buyer_queries

Поля:

- query_id;
- run_id;
- text;
- normalized_text;
- origin;
- parent_query_id;
- rationale;
- priority;
- category_id;
- category_path;
- filter_json;
- filter_hash;
- semantic_fingerprint;
- predicted_total;
- approved;
- enabled;
- state;
- pages_scheduled;
- pages_completed;
- projects_seen;
- unique_projects;
- created_at;
- updated_at.

Indexes:

- UNIQUE run/category/normalized/filter;
- run/state/priority;
- run/enabled;
- semantic fingerprint lookup.

### 10.3 buyer_query_tasks

Поля:

- task_id;
- run_id;
- query_id;
- source;
- page;
- cursor;
- request_fingerprint;
- priority;
- state;
- not_before;
- lease_owner;
- lease_deadline;
- lease_fence;
- attempt_id;
- account_registration_id;
- transport_id;
- egress_ip;
- response_artifact_id;
- result_count;
- total_hint;
- latency_ms;
- error_kind;
- error_text;
- created_at;
- completed_at.

Constraint:

- UNIQUE run_id/request_fingerprint.

### 10.4 buyer_projects

Canonical global row:

- buyer_project_id;
- platform;
- remote_project_id;
- canonical_url;
- latest_title;
- latest_description;
- latest_status;
- latest_category_id;
- buyer_remote_user_id;
- buyer_username;
- first_seen_at;
- last_seen_at;
- latest_remote_updated_at;
- canonical_hash.

Constraint:

- UNIQUE platform/remote_project_id.

### 10.5 buyer_project_observations

Immutable evidence:

- observation_id;
- project_id;
- run_id;
- query_id;
- task_id;
- attempt_id;
- worker_id;
- account_registration_id;
- transport_id;
- egress_ip;
- route_generation;
- source;
- page;
- response_position;
- observed_at;
- title;
- description;
- budget_min;
- budget_max;
- offers;
- views;
- orders;
- remote_status;
- category_id;
- parent_category_id;
- buyer_hired_percent;
- buyer_projects_count;
- buyer_active_projects_count;
- raw_artifact_id;
- normalized_hash.

### 10.6 buyer_project_matches

Поля:

- run_id;
- project_id;
- query_id;
- first_observation_id;
- last_observation_id;
- match_count;
- first_seen_at;
- last_seen_at.

Constraint:

- UNIQUE run/project/query.

### 10.7 buyer_run_projects

Materialized UI row:

- run_id;
- project_id;
- latest_observation_id;
- title;
- description_excerpt;
- budget_min/max;
- offers;
- views;
- age_seconds;
- category_id/path;
- buyer_hired_percent;
- attachment_count;
- attachment_parse_state;
- preliminary_score;
- final_score;
- matched_query_count;
- shortlist_state;
- tags_json или отдельная join table;
- proposal_state;
- conversation_state;
- unseen;
- updated_at.

Эта таблица обслуживает server-side filters/facets. Полные raw records не сканируются на каждый UI request.

### 10.8 Buyer profile и history

buyer_profiles:

- platform/user ID unique;
- username;
- current aggregate metadata.

buyer_profile_observations:

- project/run/account/source provenance;
- hired percent;
- total/active projects;
- historical projects;
- observed_at;
- raw artifact.

### 10.9 Attachments

buyer_attachments:

- attachment_id;
- project_id;
- source_observation_id;
- remote URL;
- resolved download URL;
- filename;
- declared MIME;
- detected MIME;
- size;
- SHA-256;
- auth account;
- download state;
- local object reference;
- error;
- created_at;
- downloaded_at.

buyer_attachment_derivatives:

- derivative_id;
- attachment_id;
- kind;
- parser/model name;
- parser/model version;
- content hash;
- extracted text;
- structured JSON;
- preview object reference;
- token count;
- state;
- error;
- created_at.

### 10.10 Scoring и shortlist

buyer_scores:

- project/run;
- score profile/version;
- provider/model;
- prompt version;
- input context hash;
- total score;
- feature breakdown JSON;
- rationale;
- created_at.

buyer_shortlist_items:

- run/project unique;
- state;
- rank;
- tags;
- note;
- selected_by;
- selected_at.

### 10.11 Export

buyer_exports:

- export_id;
- run_id;
- selection/filter snapshot;
- format;
- include_attachments;
- include_raw;
- state;
- progress;
- object/path;
- manifest hash;
- row count;
- bytes;
- error;
- created_at;
- completed_at.

### 10.12 Outreach и conversations

buyer_proposal_drafts:

- draft_id;
- run/project;
- account policy/account ID;
- version;
- provider/model/resolved model;
- prompt version;
- context manifest;
- context hash;
- body;
- price;
- duration;
- state;
- created_at/updated_at.

buyer_proposal_send_intents:

- send_intent_id;
- draft_id;
- project_id;
- sender_account_id;
- idempotency_key;
- draft_hash;
- state;
- created_at;
- confirmed_at.

buyer_proposal_send_attempts:

- attempt ID;
- intent ID;
- request artifact;
- response artifact;
- started/completed;
- state;
- error;
- route/account provenance.

buyer_proposal_remote_receipts:

- intent ID;
- remote offer ID, если доступен;
- has_offer evidence;
- offers snapshot;
- reconciled_at.

buyer_conversations:

- UNIQUE platform/account_registration_id/remote_dialog_id;
- project ID;
- proposal intent ID;
- buyer user ID;
- sync state;
- per-account cursor.

buyer_messages:

- conversation ID;
- remote message ID;
- direction;
- text;
- attachment manifest;
- delivery state;
- remote/local timestamps.

## 11. Raw artifacts

Все нестабильные hidden API responses сохраняются через artifact store:

- source;
- endpoint;
- request fingerprint;
- account ID;
- transport/IP/generation;
- status code;
- headers allowlist;
- response body;
- content type;
- SHA-256;
- observed_at;
- parser version.

Секреты, cookies, mobile token, CSRF и credentials не попадают в artifact.

Преимущества:

- response можно перепарсить после schema change;
- можно доказать, откуда появилось поле;
- можно сравнивать mobile/web views;
- live endpoint drift не уничтожает историю.

## 12. Фильтры и total market control

### 12.1 Server-side project filters

UI/API поддерживают:

- full text;
- run;
- category/rubric;
- matched query;
- worker;
- discovery account;
- transport;
- egress IP;
- budget min/max;
- offers min/max;
- views min/max;
- age/date range;
- buyer hired percent;
- buyer total/active projects;
- has attachments;
- attachment parse state;
- attachment type;
- preliminary/final score;
- unseen;
- shortlisted;
- selected;
- exported;
- proposal state;
- conversation state;
- duplicate/update state;
- source mobile/web;
- remote project state.

### 12.2 Sort

- newest observed;
- remote created date;
- highest score;
- lowest offers;
- highest views;
- budget;
- buyer hired percent;
- matched query count;
- attachment count;
- recent metric change.

### 12.3 Facets

Backend рассчитывает:

- category buckets;
- budget buckets;
- offers buckets;
- score buckets;
- attachment MIME/state;
- query yield;
- worker yield;
- IP/route errors;
- proposal states.

### 12.4 Scoring

Два уровня:

Preliminary deterministic:

- keyword/semantic fit;
- category fit;
- budget;
- offers competition;
- age;
- buyer hire rate;
- description completeness;
- attachment presence;
- negative rules.

Final AI:

- full description;
- buyer profile/history;
- parsed attachment context;
- operator service profile;
- feasibility;
- scope clarity;
- risk;
- expected response probability;
- suggested proposal angle.

Каждый score versioned и объясним. UI показывает breakdown, а не только число.

## 13. Attachment pipeline

### 13.1 Этапы

1. Capture manifest из web/mobile source.
2. Resolve authenticated download.
3. Download через account-scoped client.
4. Compute SHA-256, detected MIME, size.
5. Store immutable original.
6. Select parser.
7. Produce derivatives.
8. Build bounded context manifest.
9. Show parsed/skipped/error in UI.

### 13.2 Поддерживаемые типы

Первая production версия:

- PDF text;
- scanned PDF OCR;
- TXT/MD/CSV/JSON/XML;
- DOCX;
- XLSX;
- PPTX;
- PNG/JPEG/WebP/GIF first frame;
- изображения через vision/OCR;
- ZIP только manifest/listing без исполнения содержимого, после отдельной policy.

### 13.3 Ограничения

- configurable max files/project;
- max file size;
- max total bytes/project;
- max extracted text/tokens;
- timeout;
- parser isolation;
- no macro execution;
- no executable launch;
- password-protected state;
- unsupported state;
- checksum dedupe;
- explicit truncation notices.

### 13.4 Proposal context

Context manifest содержит:

- project fields;
- source freshness;
- buyer fields;
- every attachment;
- parser status;
- extracted text chunks;
- image description/OCR;
- omitted/truncated reasons;
- context hash.

Модель не должна получать ложное утверждение, что файл прочитан, если parser state не completed.

## 14. Export

### 14.1 Форматы

JSONL:

- основной полный machine-readable dataset;
- одна project record на строку;
- nested buyer, observations summary, matches, metrics, attachments, score.

Markdown:

- человекочитаемые полные карточки;
- title, URL, description, budget, offers, views, buyer, matched queries, attachments, score, notes.

TXT:

- упрощённый portable вариант Markdown без formatting dependency.

CSV:

- плоская аналитическая таблица;
- full description допустим отдельной колонкой;
- nested observations не разворачиваются полностью.

ZIP:

- manifest.json;
- projects.jsonl;
- projects.md;
- projects.csv;
- attachments/;
- attachment_derivatives/;
- README с schema/version/filter snapshot.

### 14.2 Selection

Экспорт может брать:

- весь run;
- текущий filter snapshot;
- shortlist;
- выбранные project IDs;
- unsent only;
- score threshold.

Экспорт является async durable job. UI видит progress и получает download endpoint только после checksum/manifest completion.

## 15. Proposal generation

### 15.1 Модель

Task routing:

- query planning: быстрая модель Luna/Terra или другой configured planner;
- proposal writing: configured strongest alias gpt-5.6-sol;
- conversation reply: configured strongest alias gpt-5.6-sol;
- OCR/vision: capability-specific model.

Не hardcode в generic llm_router. Использовать task env/config, например OPENAI_MODEL_PROPOSAL_WRITING и OPENAI_MODEL_CONVERSATION_REPLY, и сохранять resolved provider/model.

### 15.2 Workspace

Оператор видит:

- project description;
- current views/offers/budget;
- buyer history;
- attachment previews;
- extraction status;
- score/risk;
- service profile;
- 2-4 draft variants;
- editable body;
- price;
- duration;
- sender account;
- model/version/context hash.

### 15.3 Draft versions

- regenerate создаёт новую version;
- manual edits не уничтожают generated text;
- diff между versions;
- selected version explicit;
- context refresh invalidates stale warning, но не удаляет draft.

### 15.4 Preflight

Перед send:

- project still active;
- account session valid;
- sender account eligible;
- connects/quota sufficient;
- has_offer false;
- already_work false;
- template check;
- portfolio/kwork requirements;
- price/duration valid;
- attachments upload capability verified;
- current offers/views refreshed;
- draft context freshness visible.

### 15.5 Send state machine

~~~mermaid
stateDiagram-v2
    [*] --> Draft
    Draft --> Ready
    Ready --> Preflight
    Preflight --> Blocked
    Blocked --> Preflight
    Preflight --> Sending
    Sending --> Sent
    Sending --> Failed
    Sending --> Unknown
    Unknown --> Reconciling
    Reconciling --> Sent
    Reconciling --> Ready
    Reconciling --> ManualReview
~~~

Idempotency key:

platform/project_id/sender_account_id/draft_hash

Критическое правило: timeout после remote request не означает failed. Состояние становится unknown, затем выполняется reconciliation через has_offer, /offers или другой подтверждённый remote evidence. Blind retry запрещён.

### 15.6 Outgoing attachments

До live verification upload contract:

- UI может прикреплять local files к draft;
- preflight показывает capability unverified;
- production send с attachments блокируется feature flag.

После controlled capture:

- upload original;
- сохранить remote file token/receipt;
- attach tokens к createoffer;
- reconciliation проверяет итог;
- retry upload отдельно от createoffer.

## 16. Conversation refactor

### 16.1 Account-bound sync

Conversation identity:

platform плюс sender account плюс remote dialog.

Нужно:

- per-account inbox cursor/watermark;
- account-scoped read/send client;
- full message history, а не только последнее сообщение;
- stable remote IDs;
- message attachments;
- delivery/read state;
- project/proposal link.

### 16.2 UI

Route:

/conversations/:conversationId

Layout:

- слева список conversations;
- в центре full history и composer;
- справа AI sidecar и project context.

### 16.3 AI actions

- draft reply;
- rewrite;
- shorter;
- warmer;
- more concrete;
- summarize conversation;
- extract buyer questions;
- identify missing requirements;
- next action;
- propose price/scope clarification.

AI context:

- original project;
- sent proposal;
- all parsed attachments;
- full conversation;
- service/operator profile;
- prior commitments.

По умолчанию AI только заполняет editor. Auto-send отсутствует.

## 17. REST и WebSocket contracts

### 17.1 Runs

- POST /api/kwork/buyer-search/runs;
- GET /api/kwork/buyer-search/runs;
- GET /api/kwork/buyer-search/runs/{run_id};
- PATCH /api/kwork/buyer-search/runs/{run_id};
- POST /pause;
- POST /resume;
- POST /stop;
- GET /fleet;
- GET /workers;
- GET /operations;
- GET /events.

### 17.2 Query plan

- POST /runs/{run_id}/queries/generate;
- GET /runs/{run_id}/queries;
- PATCH /runs/{run_id}/queries/{query_id};
- POST /runs/{run_id}/queries/distribute;
- POST /runs/{run_id}/queries/regenerate-collisions;
- GET /runs/{run_id}/query-bundles.

### 17.3 Projects

- GET /runs/{run_id}/projects;
- GET /runs/{run_id}/projects/facets;
- GET /runs/{run_id}/projects/{project_id};
- POST /runs/{run_id}/projects/batch-action;
- POST /runs/{run_id}/projects/{project_id}/enrich;
- POST /runs/{run_id}/projects/{project_id}/rescore;
- GET /projects/{project_id}/attachments/{attachment_id}/preview.

List API:

- cursor pagination;
- stable sort key плюс project ID tie-breaker;
- filter snapshot;
- fields projection;
- total/facets optionally separate.

### 17.4 Exports

- POST /runs/{run_id}/exports;
- GET /runs/{run_id}/exports/{export_id};
- GET /runs/{run_id}/exports/{export_id}/download.

### 17.5 Proposals

- POST /projects/{project_id}/proposal-drafts;
- GET /proposal-drafts/{draft_id};
- PATCH /proposal-drafts/{draft_id};
- POST /proposal-drafts/{draft_id}/regenerate;
- POST /proposal-drafts/{draft_id}/preflight;
- POST /proposal-drafts/{draft_id}/send;
- POST /proposal-send-intents/{intent_id}/reconcile.

### 17.6 Conversations

- GET /api/conversations;
- GET /api/conversations/{conversation_id};
- POST /api/conversations/{conversation_id}/sync;
- POST /api/conversations/{conversation_id}/draft;
- POST /api/conversations/{conversation_id}/messages.

### 17.7 WebSocket

WS /ws/kwork-buyer-search/runs/{run_id}?after_seq=N

Events:

- run.created/started/paused/resumed/stopped/completed;
- capacity.changed;
- worker.started/state/heartbeat/draining/stopped;
- identity.bound/rebound/released;
- query.generated/approved/assigned;
- query.page.started/completed/exhausted;
- project.observed/updated;
- detail.started/completed/failed;
- attachment.discovered/downloaded/parsed/failed;
- score.completed;
- shortlist.changed;
- export.started/progress/completed/failed;
- proposal.draft.created;
- proposal.preflight.completed;
- proposal.send.started/sent/failed/unknown/reconciled;
- conversation.synced;
- message.received/sent.

WS использует sequence, replay и snapshot resync. Frontend coalesces high-frequency project/worker events каждые 100-250 ms.

## 18. Desktop UI

### 18.1 Routes и navigation

Новый пункт навигации:

- Поиск покупателей;
- icon ScanSearch или Radar.

Routes:

- /buyer-search;
- /buyer-search/runs/{runId};
- /buyer-search/runs/{runId}/projects/{projectId};
- /conversations/{conversationId}.

Seller Kwork Market остаётся отдельным экраном анализа предложения.

### 18.2 Run tabs

- Обзор;
- Запросы;
- Заказы;
- Исполнение;
- Отклики.

### 18.3 Основной workbench

~~~text
Run header: name, state, WS, progress, pause, stop, export
Tabs
KPI: workers, unique IP, queries, projects, shortlist, sent

Filter rail 264px | Virtual data grid | Inspector 420px
~~~

Project grid columns:

- selection;
- fit score;
- title;
- budget;
- offers;
- views;
- age;
- category;
- matched queries;
- buyer hired rate;
- buyer project history;
- attachments/status;
- worker/account/IP provenance;
- enrichment;
- shortlist/tags;
- proposal;
- conversation.

### 18.4 Execution view

30-row grid:

- worker;
- account;
- persona;
- VPNTEN slot;
- current egress IP;
- route mode;
- assigned query;
- page;
- state;
- requests;
- found/new/duplicates;
- latency;
- 403/429/timeouts;
- heartbeat;
- actions.

Ниже:

- query/task queue;
- retries/not_before;
- operation attempts;
- event timeline;
- capacity reasons.

### 18.5 Query view

- AI generation panel;
- category/taxonomy context;
- exact duplicate report;
- semantic collision clusters;
- query table;
- worker bundle table;
- manual enable/disable/edit;
- regenerate selected;
- redistribute;
- predicted/actual yield comparison.

### 18.6 Proposal view

- project context;
- buyer;
- attachment gallery;
- extraction manifest;
- draft variants;
- editor;
- price/duration;
- sender account;
- preflight checks;
- send audit.

### 18.7 Visual language

Feature-local tokens:

~~~css
--buyer-bg: #090d0c;
--buyer-surface: #101513;
--buyer-surface-raised: #151b18;
--buyer-line: #29312d;
--buyer-line-strong: #3a443e;
--buyer-text: #f3f1ea;
--buyer-muted: #8b938d;
--buyer-lime: #d9ff00;
--buyer-coral: #ff6652;
--buyer-cyan: #bde8ea;
--buyer-positive: #55d99b;
~~~

Правила:

- operational editorial grid;
- тёмная нейтральная база;
- chartreuse только primary/live;
- coral только block/failure/manual attention;
- cyan information/selection;
- radius 0-6 px;
- thin borders;
- почти без shadows;
- Inter для UI;
- JetBrains Mono для IDs/IP/latency/counters;
- letter spacing 0;
- без огромного hero;
- без decorative floating cards;
- Lucide icons и tooltips.

Референсы:

- C:\Users\Redmi\.codex\attachments\eb2d7eb4-a001-46cb-bbb0-26476aee1c20\image-1.png
- C:\Users\Redmi\.codex\attachments\eb2d7eb4-a001-46cb-bbb0-26476aee1c20\image-2.png

### 18.8 Frontend state

Создать:

desktop/src/features/buyer-search/

- api.ts;
- types.ts;
- state/;
- hooks/;
- pages/;
- components/launch/;
- components/queries/;
- components/projects/;
- components/execution/;
- components/outreach/.

Правила:

- backend DB source of truth;
- feature-local typed API;
- reducer хранит только UI state;
- filters/sort/open project отражаются в URL;
- cursor pagination;
- row virtualization;
- lazy detail/attachments;
- AbortController;
- typed error envelope;
- no dataset localStorage;
- WS replay/resync;
- snapshot reconciliation;
- event coalescing.

### 18.9 Responsive/accessibility

- 1600+: rail, grid, inspector одновременно;
- 1280-1599: collapsible rail, inspector 360-400;
- 1024-1279: drawers;
- меньше 1024: stacked details, sticky action bar;
- grid может иметь horizontal scroll;
- global sidebar на mobile должен стать compact icon rail, а не занимать 48vh.

Accessibility:

- не использовать clickable tr;
- отдельная open button;
- checkbox selection;
- aria-label/tooltips для icon-only controls;
- focus trap/restore;
- status не только цветом;
- aria-live только для важных lifecycle events;
- keyboard query reorder/assignment;
- virtual row indexes;
- prefers-reduced-motion.

## 19. Требования к VPNTEN

Это отдельный backlog для владельца VPNTEN. PSR не меняет его внутренний код.

### 19.1 Обязательная capacity

- минимум 30 одновременно running и healthy instances;
- минимум 30 уникальных одновременно подтверждённых outbound IP;
- stable slot IDs;
- stable proxyUrl на время lease;
- независимый start/stop/rotate каждого slot;
- prewarm 30 instances.

Количество записей slots само по себе не считается capacity. Capacity равна числу свежих healthy unique egress IP.

### 19.2 /instances

Для каждого slot:

- slot;
- proxyUrl;
- running;
- readiness;
- health;
- generation;
- profileId/profileName;
- country;
- pid;
- startedAt;
- updatedAt;
- egressIp;
- egressCheckedAt;
- lastError;
- degradationReason;
- lastRotateReason;
- lease owner/token/expiry, если reservation реализуется в VPNTEN.

Aggregate:

- total;
- running;
- ready;
- healthy;
- uniqueEgress;
- duplicateEgress;
- starting;
- degraded;
- quarantined.

### 19.3 Commands

- GET /instances;
- GET /status?slot=N;
- POST /start?slot=N;
- POST /rotate?slot=N;
- POST /stop?slot=N.

Желательно:

- POST /instances/prewarm count=30;
- POST /instances/status-batch;
- POST /instances/reserve;
- POST /instances/renew;
- POST /instances/release.

### 19.4 Atomic reservation

Для нескольких PSR/API процессов желательно:

- reserve(slot, owner, ttl);
- leaseToken;
- expiresAt;
- compare-and-swap renew/release;
- generation token;
- конфликт отдаёт 409;
- истёкшая lease освобождается;
- owner виден в /instances.

Текущий in-memory lease owner внутри PSR VPNTEN adapter безопасен только для одного процесса.

### 19.5 Egress verification

- IP проверяется через внешний endpoint из самого proxy;
- egressCheckedAt обязателен;
- stale IP не считается healthy;
- duplicate IP между slots явно помечается;
- route change увеличивает generation;
- после rotate old IP не возвращается как fresh;
- no silent direct path;
- IP verification failure сообщает reason.

### 19.6 Affinity hints

VPNTEN может принимать:

- preferredEgressIp;
- preferredSlot;
- accountHint.

Но это hints. Поведение:

- если preferred IP свободен и healthy, использовать его;
- если занят, вернуть другой свободный healthy unique route;
- не блокировать account только из-за несовпадения preferred IP;
- не отдавать route, уже leased другому worker.

### 19.7 Rotation

- idempotency key;
- asynchronous status до ready;
- old generation invalid;
- причина rotation;
- drain acknowledgement;
- slot independently unavailable while rotating;
- no collateral rotation других slots.

## 20. План файлового рефакторинга

### 20.1 Runtime

Изменить:

- src/platforms/kwork_supply/models.py;
- src/platforms/kwork_supply/repository.py;
- src/platforms/kwork_supply/worker.py;
- src/platforms/kwork_supply/supervisor.py;
- src/platforms/kwork_supply/coordinator.py;
- src/platforms/kwork_supply/identity_pool.py;
- src/platforms/kwork_supply/executor.py.

Добавить generic workflow registry и fencing, не менять seller behavior.

### 20.2 Buyer backend

Добавить:

- src/platforms/kwork_buyer/__init__.py;
- models.py;
- repository.py или buyer-specific section/adaptor;
- workflow.py;
- query_planner.py;
- query_normalizer.py;
- sources/mobile_projects.py;
- sources/web_projects.py;
- sources/capabilities.py;
- mapper.py;
- enrichment.py;
- scoring.py;
- attachments.py;
- export.py;
- events.py;
- service.py.

### 20.3 API

Добавить:

- src/api/routes/kwork_buyer_search.py;
- src/api/routes/kwork_buyer_outreach.py;
- typed response/request schemas;
- WS replay route.

Legacy src/api/routes/kwork.py buyer-scout оставить deprecated до shadow parity.

### 20.4 Outreach

Рефакторить:

- src/action/proposal_db.py;
- src/action/proposal_generator.py;
- src/action/proposal_sender.py;
- src/utils/reply_linker.py;
- src/utils/inbox_monitor.py;
- src/api/routes/dashboard.py;
- src/api/routes/candidates.py.

Добавить account-scoped service, outbox/send intent и reconciliation.

### 20.5 Desktop

Добавить:

- desktop/src/features/buyer-search/;
- routes в desktop/src/App.tsx;
- nav в desktop/src/components/Layout.tsx;
- conversation feature module.

Переиспользовать через neutral components:

- category picker;
- account team picker;
- fleet widgets;
- durable event stream.

Не импортировать buyer semantics из seller resultModel/PublicationWorkspace.

## 21. Пошаговый implementation plan

### Phase 0. Baseline и correctness repair

Задачи:

1. Зафиксировать baseline tests и DB backup/migration strategy.
2. Исправить ProposalDB migration: replied_at и все поля/methods reply_linker.
3. Исправить Windows temp DB cleanup в smoke tests, чтобы первый schema failure не создавал cascade lock failures.
4. Добавить lease fencing.
5. Добавить единый operation+identity heartbeat.
6. Убрать 250 ms write polling.
7. Добавить writer queue/batching metrics.
8. Перевести synchronous transport refresh с event loop.
9. Закрепить signup IP fallback test.

Acceptance:

- current seller market tests green;
- phase-3 feedback smoke green;
- stale worker commit test green;
- 30 idle workers не создают write storm;
- long handler не теряет identity lease;
- occupied signup IP даёт fallback, не blocked.

### Phase 1. Generic workflow seam

Задачи:

1. Добавить job_kind/config_json.
2. Добавить workflow registry.
3. Обернуть supply workflow без функционального изменения.
4. Добавить buyer operation enums.
5. Добавить generic event namespace.

Acceptance:

- старые jobs читаются как supply;
- seller API/UI не меняет поведение;
- buyer empty run создаётся, pause/resume/stop/recovery работает.

### Phase 2. Buyer schema и read API skeleton

Задачи:

1. Создать buyer tables/indexes.
2. Создать run/query/task/project repositories.
3. Добавить raw artifact storage.
4. Добавить list/detail/facets API.
5. Добавить WS snapshot/replay.

Acceptance:

- migrations idempotent;
- canonical project dedupe;
- multi-query observations;
- cursor pagination stable;
- WS recovers after restart.

### Phase 3. Account-scoped read-only sources

Задачи:

1. Capability-scoped buyer read client.
2. Mobile /projects source.
3. Mobile /want and /project enrichment sources.
4. Web /projects state source.
5. Buyer history source.
6. Read-only denylist enforcement.
7. Per-endpoint auth/error normalization.

Acceptance:

- каждый request содержит worker account/transport provenance;
- mutation endpoint нельзя вызвать из discovery workflow;
- raw and normalized records stored;
- same project across sources merges canonically.

### Phase 4. AI Query Planner

Задачи:

1. Taxonomy snapshot service.
2. Brief/category/manual/hybrid inputs.
3. LLM structured output schema.
4. Exact normalization/dedupe.
5. Semantic collision analysis.
6. Suggest expansion.
7. Approve/edit/disable UI API.
8. Durable distribution.

Acceptance:

- минимум workers × queries_per_worker approved unique queries;
- DB rejects duplicate;
- all queries have origin/rationale/category;
- UI shows collision report and assignments.

### Phase 5. 30-worker discovery

Задачи:

1. Buyer FETCH operation handler.
2. Page/task scheduling.
3. Work stealing.
4. Batch commit.
5. global project dedupe;
6. adaptive concurrency applied;
7. 403/429/timeout policy;
8. capacity preflight and elastic scaling.

Canary:

- 2 workers;
- 5;
- 10;
- 20;
- 30.

Acceptance at 30:

- 30 distinct account IDs;
- 30 distinct transport IDs;
- 30 distinct fresh egress IP;
- one in-flight request/worker;
- no duplicate task execution after stable completion;
- no SQLITE_BUSY;
- no identity collision;
- UI event lag within gate.

### Phase 6. Desktop run control

Задачи:

1. New navigation/routes.
2. Scoped visual tokens.
3. Run launcher.
4. Query view.
5. Overview KPIs.
6. 30-worker execution grid.
7. WS reducer/replay.
8. responsive shell.

Acceptance:

- operator видит реальную capacity и причину shortfall;
- каждый worker/account/IP/query прозрачен;
- pause/resume/stop переживает reload;
- no localStorage dataset.

### Phase 7. Project database, filters и shortlist

Задачи:

1. buyer_run_projects materialization.
2. Server-side filters/sort/facets.
3. Virtualized grid.
4. Inspector.
5. tags/saved views.
6. bulk shortlist actions.
7. provenance and metric history.

Acceptance:

- 10k+ rows остаются responsive;
- filters reproducible через URL;
- views/offers source and freshness видны;
- one canonical project может показать все matched queries.

### Phase 8. Enrichment и attachments

Задачи:

1. Selective enrichment policies.
2. /want detail.
3. web state/files.
4. buyer history.
5. authenticated download.
6. PDF/DOCX/XLSX/PPTX/text/image parsers.
7. OCR/vision.
8. preview proxy.
9. context manifest.

Acceptance:

- fixture corpus проходит;
- unsupported/oversized/corrupt states видны;
- source file checksum preserved;
- model context точно показывает omissions.

### Phase 9. Scoring и export

Задачи:

1. deterministic preliminary scoring.
2. versioned AI scoring.
3. score breakdown UI.
4. JSONL/Markdown/TXT/CSV/ZIP jobs.
5. manifest/checksum.

Acceptance:

- export содержит full description;
- attachment manifest complete;
- filter/selection snapshot reproducible;
- same input/version gives deterministic structure.

### Phase 10. Proposal composer

Задачи:

1. Promote selected project to outreach domain idempotently.
2. Versioned drafts.
3. gpt-5.6-sol task alias.
4. full attachment-aware context.
5. price/duration controls.
6. preflight.
7. send intent/outbox.
8. remote receipt/reconciliation.
9. controlled upload capability gate.

Acceptance:

- crash after remote acceptance не вызывает duplicate;
- exact model/context hash stored;
- stale project warning;
- sender account explicit;
- send audit complete.

### Phase 11. Account-bound conversations

Задачи:

1. conversation schema migration.
2. per-account sync cursors.
3. full history and attachments.
4. typed realtime UI.
5. project/proposal context.
6. AI sidecar.
7. remove global send route usage.

Acceptance:

- два accounts с одинаковым buyer/dialog не смешиваются;
- incoming message появляется в правильном conversation;
- AI sees full context;
- send uses pinned sender account;
- no auto-send.

### Phase 12. Shadow rollout и legacy retirement

Задачи:

1. Shadow compare legacy scout/new run.
2. Field parity report.
3. Endpoint drift alarms.
4. operator acceptance.
5. deprecate legacy route/UI.
6. read compatibility for old ProposalDB data.

Acceptance:

- new flow полностью покрывает required use case;
- no silent data loss;
- rollback documented;
- legacy code удаляется отдельным PR после observation period.

## 22. Test plan

### 22.1 Unit

- query normalization;
- exact/semantic dedupe;
- request fingerprint;
- IP candidate ranking;
- signup IP occupied fallback;
- lease fence;
- state transitions;
- source mappers;
- attachment MIME dispatch;
- context truncation;
- score versioning;
- export serialization;
- send idempotency.

### 22.2 Integration

- 30 workers/30 accounts/30 IP;
- run pause/resume/restart;
- worker crash/reassignment;
- stale commit rejected;
- long identity heartbeat;
- project multi-query merge;
- page retry;
- 403/429 policy;
- attachment download auth;
- proposal unknown/reconcile;
- multi-account conversation sync;
- WS replay/resync.

### 22.3 Frontend

Добавить Vitest и React Testing Library:

- reducers;
- filter URL state;
- WS replay reducer;
- selection;
- query collision controls;
- proposal preflight states.

Добавить Playwright:

- create category run;
- approve queries;
- start/pause/resume;
- inspect 30-worker view;
- filter/select/export;
- open attachment;
- generate/edit/preflight/send mock proposal;
- open conversation and generate draft.

### 22.4 Performance

Scenarios:

- 30 idle workers;
- 30 simultaneous page responses;
- 360 projects in one wave;
- 10k project run;
- 100 attachment enrichments;
- 100 concurrent WS clients не требуется; минимум desktop plus test clients;
- export 10k rows plus attachments.

Metrics:

- requests/sec;
- remote latency by endpoint;
- DB commit p50/p95/p99;
- writer queue;
- WS event lag;
- duplicate ratio;
- unique yield/query;
- worker utilization;
- 403/429 rate;
- route rotation;
- attachment throughput;
- LLM latency/cost.

## 23. Live verification gates

До production mutation:

1. Найти project с непустым files[].
2. Сохранить exact attachment schema.
3. Скачать каждый доступный file type.
4. Сравнить /want.views и web views_dirty на одном ID.
5. Получить непустой /offers response.
6. Capture controlled successful createoffer request/response.
7. Проверить duplicate protection через has_offer.
8. Capture inboxCreate, history pagination и attachments.
9. Проверить token TTL/refresh/fcmTokenLost.
10. Проверить classifier filtering.
11. Повторно проверить sort=date.
12. Canary 2, 5, 10, 20, 30 unique accounts/IP.
13. Проверить route generation и stale IP invalidation.
14. Переизвлечь buyer JS endpoint map в валидный JSON без duplicate keys.

## 24. Наблюдаемость

Structured logs:

- run_id;
- operation/task/query/project;
- worker/account;
- transport/slot/IP/generation;
- endpoint;
- attempt/fence;
- status/error;
- latency;
- result/new/duplicate count.

Metrics:

- buyer_run_effective_workers;
- buyer_unique_egress;
- buyer_query_queue_depth;
- buyer_projects_unique_total;
- buyer_project_duplicate_ratio;
- buyer_request_latency;
- buyer_request_errors;
- buyer_writer_queue_depth;
- buyer_commit_latency;
- buyer_ws_lag;
- buyer_attachment_states;
- buyer_proposal_states;
- buyer_conversation_sync_lag.

Audit:

- config versions;
- query generations;
- manual edits;
- shortlist changes;
- export manifests;
- proposal draft versions;
- send confirmation;
- remote reconciliation;
- AI model/context hashes.

## 25. Migration и rollback

### 25.1 Database

- additive migrations first;
- backup before schema change;
- migration version table;
- no destructive ProposalDB rewrite;
- backfill old candidates/proposals into compatibility views where possible;
- new buyer DB can initially live in same SQLite file with isolated tables;
- rollback disables new routes/workflow without deleting data.

### 25.2 Feature flags

- BUYER_SEARCH_ENABLED;
- BUYER_SEARCH_LIVE_DISCOVERY;
- BUYER_SEARCH_MAX_WORKERS;
- BUYER_ATTACHMENT_DOWNLOAD;
- BUYER_ATTACHMENT_VISION;
- BUYER_PROPOSAL_SEND;
- BUYER_PROPOSAL_ATTACHMENTS;
- BUYER_CONVERSATION_SYNC.

### 25.3 Rollback triggers

- identity collision;
- duplicate send;
- stale commit accepted;
- unexpected mutation from discovery;
- sustained DB lock/backlog;
- route direct fallback;
- cross-account conversation mix;
- unbounded 403/429 escalation.

## 26. Definition of Done

Функция считается готовой, когда:

- существует отдельная вкладка Поиск покупателей;
- run создаётся из brief/category/manual inputs;
- AI query plan полностью видим и редактируем;
- query uniqueness enforced;
- 30 workers могут работать параллельно при реальной VPNTEN capacity;
- каждый worker использует отдельный account и unique IP;
- занятый signup IP приводит к fallback, а не к blocked;
- all projects durable и globally deduplicated;
- views/offers/budget/source freshness видны;
- server-side filters и shortlist работают;
- full dataset экспортируется в JSONL и Markdown, CSV/ZIP доступны;
- attachments скачиваются и парсятся с manifest;
- proposal генерируется configured strongest model;
- send имеет preflight, idempotency и reconciliation;
- conversation закреплён за sender account;
- AI помогает в чате без auto-send;
- pause/resume/restart/replay работают;
- tests, performance gates и live canary пройдены;
- legacy buyer scout помечен deprecated и имеет retirement plan.

## 27. Рекомендуемая последовательность первых PR

PR 1: runtime correctness

- fencing;
- heartbeat;
- event-driven workers;
- SQLite metrics;
- ProposalDB phase-3 repair;
- IP fallback regression tests.

PR 2: generic job_kind и empty buyer workflow

- migrations;
- workflow registry;
- run lifecycle;
- REST/WS skeleton.

PR 3: buyer schema и read-only sources

- projects list;
- canonical DB;
- observations;
- raw artifacts;
- list/detail API.

PR 4: query planner и category mode

- taxonomy;
- structured LLM output;
- dedupe;
- distribution;
- query UI.

PR 5: 30-worker discovery и execution UI

- handler;
- paging;
- capacity;
- worker grid;
- canary up to current VPNTEN capacity.

PR 6: projects workbench

- filters;
- facets;
- grid;
- inspector;
- shortlist.

PR 7: enrichment, attachments и scoring

- details/history/files;
- parsers/OCR/vision;
- context manifest;
- scoring.

PR 8: export

- JSONL/Markdown/TXT/CSV/ZIP.

PR 9: proposal outbox

- drafts;
- strongest model;
- preflight;
- send/reconcile;
- controlled live gate.

PR 10: conversations

- account-bound sync;
- realtime UI;
- AI sidecar.

## 28. Проверенная исходная база и известные блокировки

Проверено в ходе аудита:

- tests/unit/test_market_identity_pool.py: 8 passed;
- current allocator уже выбирает fallback при занятом preferred IP;
- dashboard conversation tests: 10 passed;
- proposal attachment tests: 2 passed;
- market resume integration: 2 passed.

Известная baseline проблема:

- tests/smoke/test_phase3_feedback_loop.py: 6 failed, 1 passed;
- первая содержательная причина: legacy schema не содержит replied_at;
- последующие Windows temporary DB lock failures являются каскадом после первого падения;
- это входит в обязательный Phase 0, а не откладывается до chat phase.

## 29. Финальный вывод

Discovery API уже достаточно хорошо изучен, чтобы начинать read-only реализацию: POST /projects является быстрым основным collector, /want и authenticated web state используются для selective enrichment.

Главные риски находятся не в поиске:

- фактическая capacity сейчас только 10 unique verified IP;
- runtime требует fencing и снижения SQLite write churn;
- attachments mutation contract не подтверждён live;
- proposal send требует outbox/reconciliation;
- chats требуют account affinity;
- legacy ProposalDB feedback schema сейчас сломана.

Первый инженерный шаг: Phase 0 runtime correctness. Первый продуктовый vertical slice после него: category mode → AI unique queries → 2-5 workers → durable projects table → UI filters. После прохождения canary система масштабируется 10 → 20 → 30 workers без смены архитектуры.

# Kwork Market: план перестройки обхода, workers и control plane

Дата: 2026-07-11
Статус: архитектурный план, рабочий код в рамках этого аудита не менялся
Область: рынок предложений Kwork, сбор карточек, VPNTE transport pool, live-наблюдаемость и управление из Desktop UI

## 1. Итоговое решение

Текущий supply scanner нельзя масштабировать простым увеличением числа запросов или concurrency.

Причины:

1. Mobile endpoint `POST /kworks` в текущем контракте не листает выдачу по `page`: запросы `page=1` и `page=2` возвращают одинаковые ID, а ответ сообщает `paging.page=1`.
2. Текущие «воркеры» являются временными coroutine, использующими общий semaphore и массив HTTP-клиентов. У них нет identity, heartbeat, lifecycle, очереди команд, durable lease или возможности восстановиться после рестарта.
3. UI делает один долгий POST и до его завершения знает только `marketScanLoading=true`.
4. Новый supply path почти не использует уже исследованные источники из `docs`: taxonomy, aggregate filters, category attributes, canonical web aliases и реальный web load-more.
5. Рабочий проход и глубокий проход сейчас не являются стадиями одной сохраняемой задачи.

Целевая система должна быть построена вокруг следующих решений:

- запуск анализа создаёт durable job и немедленно возвращает `202 + job_id`;
- реальные workers являются долгоживущими адресуемыми actors;
- один worker владеет одним transport lease и максимум одной активной сетевой операцией;
- VPNTE slots 1-10 становятся управляемыми transport entities, а не предполагаемым диапазоном портов;
- карта рубрики строится дешёвыми aggregate/taxonomy API;
- карточки собираются только через source adapter с доказанным continuation contract;
- текущим основным card source должен стать web endpoint `/catalog_kworks_filters/{alias}` с cursor `excludeIds + onePage=1`;
- mobile `/kworks` остаётся источником первой страницы, counts и classifier tree, пока его пагинация не будет доказана отдельно;
- progress, workers, requests, errors и checkpoints транслируются через replayable WebSocket events;
- SQLite WAL является локальным durable store для Desktop-версии;
- broker/runtime interfaces сразу отделяются, чтобы позднее перейти на Redis Streams, PostgreSQL или удалённые worker processes без замены UI-контракта;
- рабочий, глубокий и полный проход продолжают одну job, меняя target и budget, а не начинают сбор заново.

## 2. Что подтверждено аудитом

### 2.1 Текущий dataflow

| Этап | Текущее поведение | Доказательство |
|---|---|---|
| UI | Один boolean loading, один ожидаемый POST | [KworkMarket.tsx](C:/psr/desktop/src/pages/KworkMarket.tsx:1396) |
| API client | Обычный request к `/api/kwork/market/supply-scan` | [api.ts](C:/psr/desktop/src/lib/api.ts:178) |
| Backend route | Scanner создаётся внутри request handler, полностью await-ится и закрывается | [kwork.py](C:/psr/src/api/routes/kwork.py:768) |
| Transport pool | Считываются URL/порты, создаются clients и `Semaphore(1)` | [kwork_market_supply.py](C:/psr/src/platforms/kwork_market_supply.py:113), [kwork_market_supply.py](C:/psr/src/platforms/kwork_market_supply.py:394) |
| Выполнение | Временные coroutine запускаются batch-ами через `asyncio.gather` | [kwork_market_supply.py](C:/psr/src/platforms/kwork_market_supply.py:582) |
| Состояние | Все counters и результаты живут в локальных переменных одного вызова | [kwork_market_supply.py](C:/psr/src/platforms/kwork_market_supply.py:595) |
| AI | LLM вызывается до завершения того же HTTP request | [kwork_market_supply.py](C:/psr/src/platforms/kwork_market_supply.py:746) |
| Persistence | Только в конце создаётся assistant context и итоговый JSON snapshot | [kwork_market_supply.py](C:/psr/src/platforms/kwork_market_supply.py:749) |

Это объясняет, почему UI нельзя честно дополнить «статусами воркеров» без перестройки backend.

### 2.2 Дефект mobile pagination

Ограниченная live-проверка в ходе аудита:

| classifier | requested page | response page | cards | результат |
|---:|---:|---:|---:|---|
| 1587 | 1 | 1 | 10 | baseline |
| 1587 | 2 | 1 | 10 | те же 10 ID |
| 1271 | 1 | 1 | 10 | baseline |
| 1271 | 2 | 1 | 10 | те же 10 ID |

Дополнительная матрица передачи параметров не нашла простого transport fix: `page=2` через POST query и через form body в обоих случаях вернул `paging.page=1` и одинаковую первую выдачу; JSON body вернул API `success=false`. Поэтому нельзя исправить текущий обход одной заменой query на form.

Код действительно передаёт `page`, поэтому проблема находится в фактическом endpoint contract, а не в отсутствии аргумента в Python:

- [get_kworks](C:/psr/src/platforms/kwork_market.py:1230)
- [scanner fetch](C:/psr/src/platforms/kwork_market_supply.py:430)

Scanner не проверяет:

- совпадает ли requested cursor с reported cursor;
- изменился ли fingerprint страницы;
- сколько новых уникальных ID дала операция;
- не застрял ли source на первой выдаче.

Сохранённый проход показывает последствие:

- 54 запланированных и 54 успешных request;
- 540 card occurrences;
- только 60 unique cards;
- 480 повторных occurrences, то есть 88,9%;
- у retained cards записана последняя requested page, потому что dedupe перезаписывает объект.

Артефакт: [kwork_supply_20260710T192859Z.json](C:/psr/docs/kwork_market_supply_snapshots/kwork_supply_20260710T192859Z.json:30).

### 2.3 Рабочий web continuation

В извлечённом Kwork JS load-more строится так:

- `excludeIds=this.getExcludedKworks()`;
- `onePage=1`;
- `getExcludedKworks()` возвращает CSV уже показанных ID.

Источник: [kwork_js_priority_endpoint_context_2026-07-08T145218Z.json](C:/psr/docs/kwork_js_priority_endpoint_context_2026-07-08T145218Z.json:881).

Live-проверка для canonical alias `website-repair`, который вернул `activeCategoryId=38`:

| Запрос | Returned cards | Пересечение с предыдущими | Новых unique |
|---|---:|---:|---:|
| первая web-пачка | 24 | - | 24 |
| `excludeIds=<24 ids>, onePage=1` | 24 | 0 | 24 |
| продолжение в расширенной проверке | 6 пачек по 24 | 0 | 144 |

Дополнительные факты:

- обычные `page=1` и `page=2` на web endpoint дали тот же набор из 24 ID в другом порядке;
- requested `pageSize=50` фактически дал `items_per_page=24`;
- ответ для category 38 сообщил `total_found около 13 000`, но `total=1000`;
- следовательно, размер batch и limit нельзя выводить из request settings;
- для 10 000 карточек потребуется partitioning, потому что один web stream, вероятно, ограничен первой тысячей результатов.

### 2.4 Что уже знает проект, но supply scanner не использует

| Источник | Уже подтверждённая ценность | Как должен использоваться |
|---|---|---|
| `catalogRubrics` | Дешёвый список корневых рубрик | Mapping phase |
| `catalogCategories(rubricId)` | Child categories и category counts | Mapping phase |
| `catalogMainv2` | Curated category/classifier seeds и counts | Planning hints |
| `catalogFilters(categoryId)` | Aggregate count, price bounds, seller/activity dimensions | Aggregate map и shard candidates |
| `categoryAttributes(category_id)` | Classification tree, usage, child values, counts | Основной источник classifier partitions |
| mobile `/kworks` | Первая page, counts, classifiers, card schema | First-page evidence и cross-check |
| web `/catalog_kworks_filters/{alias}` | Web filter state и рабочий continuation | Primary card collection |
| `getKworkDetails*`, seller/reviews | Глубокое enrichment | Только после сбора, с лимитом |
| buyer `/projects`, `/wants` | Спрос | Отдельная job/pipeline, не supply coverage |

Основные документы:

- [kwork_hidden_api_facts.md](C:/psr/docs/kwork_hidden_api_facts.md:750)
- [kwork_api_inspection_report.md](C:/psr/docs/kwork_api_inspection_report.md:811)
- [kwork_market_rebuild_journal.md](C:/psr/docs/kwork_market_rebuild_journal.md:35)
- [kwork_proxy_rotation_notes.md](C:/psr/docs/kwork_proxy_rotation_notes.md:5)

### 2.5 Что в текущем коде стоит сохранить

Полная перепись не должна уничтожить удачные решения:

- supply и buyer demand уже логически разделены;
- round-robin по slices полезен для репрезентативного частичного прохода;
- один in-flight request на transport slot является правильным ограничением;
- cache, backoff и явная остановка на protection signal нужны;
- category total уже отделён от observed sample;
- raw API evidence сохраняется для анализа;
- existing WebSocket infrastructure и UI master-detail pattern можно переиспользовать;
- assistant context можно оставить как consumer готового job snapshot.

## 3. Цели и архитектурные инварианты

### 3.1 Функциональные цели

Система должна:

1. Показывать каждую активную job, worker, transport и operation в реальном времени.
2. Показывать, куда подключён worker:
   - local proxy slot и port;
   - VPNTE profile и country;
   - session source;
   - target host и endpoint;
   - category, shard и continuation cursor.
3. Позволять из UI:
   - pause/resume job;
   - graceful/force stop;
   - менять desired worker count;
   - drain/restart/disable worker;
   - rotate/reconnect transport;
   - retry конкретную operation.
4. Восстанавливаться после рестарта backend.
5. Продолжать глубокий проход из checkpoint рабочего прохода.
6. Реально набирать 500, 2 000 и 10 000 unique cards при доступности источника.
7. Не смешивать aggregate volume, card coverage, sample metrics и AI hypotheses.

### 3.2 Инварианты корректности

- Ни один source не считается paginated только потому, что request содержит `page`.
- У каждой сетевой операции сохраняются requested cursor и reported cursor.
- Progress по карточкам считается только по global unique IDs.
- Execution semantics: at-least-once operation execution плюс idempotent commit.
- Duplicate response не повреждает first-seen provenance.
- 403/429/CAPTCHA/timeout не считаются exhaustion.
- Silent direct/proxy fallback запрещён: фактический transport всегда виден и записан.
- Один transport slot не выполняет более одной сетевой операции одновременно.
- Rotate/stop transport выполняется только после drain соответствующего worker.
- UI не является источником истины; state хранится в backend.
- Tokens, cookies, passwords и proxy credentials никогда не попадают в events или UI.
- Aggregate API count никогда не называется «охватом карточек».

## 4. Целевая архитектура

~~~mermaid
flowchart LR
    UI["Desktop UI"] -->|REST commands| API["Market Jobs API"]
    API --> COORD["MarketScanCoordinator"]
    UI <-->|Replayable WebSocket| EVENTS["Market Event Stream"]

    COORD --> STORE["SQLite WAL Job Store"]
    COORD --> PLANNER["Scope Mapper + Shard Planner"]
    COORD --> SUP["Worker Supervisor"]
    STORE --> EVENTS

    SUP --> W1["ScannerWorker 01"]
    SUP --> W2["ScannerWorker 02"]
    SUP --> WN["ScannerWorker N"]

    W1 --> TM["Transport Manager"]
    W2 --> TM
    WN --> TM
    TM --> VPNTE["VPNTE slots 1..10"]

    W1 --> SOURCES["Source Adapters"]
    W2 --> SOURCES
    WN --> SOURCES

    SOURCES --> AGG["Taxonomy/Aggregate APIs"]
    SOURCES --> MOBILE["Mobile first-page API"]
    SOURCES --> WEB["Web catalog continuation"]
    SOURCES --> ENRICH["Selective enrichment"]

    STORE --> ANALYZER["Metrics + AI Analyzer"]
    ANALYZER --> STORE
~~~

### 4.1 Компоненты

| Компонент | Ответственность |
|---|---|
| `MarketScanCoordinator` | Создание job, state transitions, phase orchestration, finalization |
| `MarketJobRepository` | Durable state, leases, checkpoints, listings, events |
| `ScopeMapper` | Taxonomy, aggregate counts, filter dimensions, alias validation |
| `ShardPlanner` | Partition plan, priority, budgets, dynamic expansion |
| `WorkerSupervisor` | Desired worker reconciliation, restart, recovery, heartbeats |
| `ScannerWorker` | Долгоживущий actor, claim/execute/commit loop |
| `TransportManager` | VPNTE instances, health, leases, rotation, quarantine |
| `SourceAdapter` | Source-specific cursor, request, response validation, normalization |
| `MetricsProjector` | Job/slice/worker metrics без LLM |
| `MarketAnalyzer` | Quantiles, structured evidence, AI hypotheses |
| `EventHub` | Durable events, live fan-out, replay by sequence |
| `SnapshotExporter` | Итоговые JSON/JSONL/GZIP artifacts |

## 5. Domain model

### 5.1 Job

`MarketScanJob` представляет весь жизненный цикл исследования одной scope.

Обязательные поля:

- `job_id`;
- selected category/classifier;
- canonical alias;
- execution profile;
- target unique cards;
- request/time budgets;
- desired workers;
- network policy;
- source policy;
- state и phase;
- current revision;
- aggregate metrics;
- created/started/finished timestamps;
- latest checkpoint;
- last error/warning.

### 5.2 Shard

Shard является логически отдельным stream выдачи.

Примеры:

- category 38 без фильтра;
- classifier 1271;
- classifier 1587;
- classification attribute value;
- доказанный непересекающийся price interval;
- отдельный canonical nested alias.

Shard хранит:

- source adapter;
- canonical alias;
- normalized filters;
- declared/estimated count;
- opaque cursor;
- state;
- priority;
- received occurrences;
- global new unique;
- duplicate count;
- consecutive zero-novelty count;
- last response fingerprint;
- protection/cooldown status.

### 5.3 Worker

Worker является адресуемой execution entity.

Поля:

- stable `worker_id`;
- `generation`, увеличиваемая при restart;
- runtime kind;
- desired state;
- actual state;
- heartbeat;
- transport lease;
- current operation;
- started/stopped timestamps;
- counters;
- rolling latency;
- last error;
- control command revision.

### 5.4 Transport

Transport не является worker.

Поля:

- `transport_id`;
- kind: direct или VPNTE;
- VPNTE slot;
- local proxy URL/port;
- profile ID/name;
- country;
- process PID;
- startedAt;
- health state;
- health details;
- generation;
- lease owner;
- quarantine deadline;
- last rotation;
- last successful Kwork probe.

### 5.5 Operation и Attempt

Operation — durable единица работы:

- `map_scope`;
- `resolve_alias`;
- `fetch_batch`;
- `enrich_listing`;
- `analyze_snapshot`;
- `export_snapshot`.

Attempt хранится отдельно, чтобы retry не стирал историю:

- worker и transport;
- request params после redaction;
- requested cursor;
- response cursor;
- status code;
- duration;
- response bytes;
- received/new/duplicate counts;
- page fingerprint;
- raw artifact reference;
- failure kind;
- retry-after;
- started/finished timestamps.

### 5.6 Listing и Observation

`listing` хранит нормализованную текущую версию карточки.

`listing_observation` хранит append-only provenance:

- operation/attempt;
- source;
- shard;
- response position;
- requested/reported cursor;
- observed_at;
- raw artifact reference.

Это устраняет текущий дефект, при котором повторная карточка перезаписывает своё происхождение последней requested page.

### 5.7 Event и Checkpoint

Event имеет монотонный `seq` внутри job.

Checkpoint содержит:

- job revision;
- phase;
- shard cursors;
- open/leased operations;
- dedupe totals;
- metrics;
- desired worker count;
- last event sequence;
- timestamp.

## 6. State machines

### 6.1 Job state

~~~mermaid
stateDiagram-v2
    [*] --> preparing
    preparing --> mapping
    mapping --> planning
    planning --> running
    running --> pausing
    pausing --> paused
    paused --> running
    running --> completing
    completing --> enriching
    enriching --> analyzing
    analyzing --> finalizing
    finalizing --> completed
    completed --> running: extend target
    running --> stopping
    paused --> stopping
    stopping --> stopped
    preparing --> failed
    mapping --> blocked
    running --> blocked
    blocked --> running: user action / cooldown
    running --> failed
~~~

Job phase и job state должны быть раздельными:

- state отвечает на вопрос «можно ли выполнять работу»;
- phase отвечает на вопрос «какой тип работы сейчас выполняется».

### 6.2 Worker state

~~~mermaid
stateDiagram-v2
    [*] --> starting
    starting --> connecting
    connecting --> idle
    idle --> leasing
    leasing --> busy
    busy --> idle
    busy --> backoff
    backoff --> idle
    busy --> blocked
    blocked --> connecting
    idle --> draining
    busy --> draining
    draining --> stopped
    stopped --> starting: restart
    starting --> crashed
    connecting --> crashed
    busy --> crashed
~~~

Рекомендуемые actual states:

- `starting`;
- `connecting`;
- `idle`;
- `leasing`;
- `busy`;
- `cooldown`;
- `backoff`;
- `blocked`;
- `draining`;
- `stopped`;
- `crashed`.

### 6.3 Operation state

- `queued`;
- `leased`;
- `running`;
- `succeeded`;
- `retry_wait`;
- `failed`;
- `cancelled`;
- `contract_violation`;
- `blocked`.

### 6.4 Семантика команд

| Команда | Семантика |
|---|---|
| Pause job | Перестать выдавать новые leases, дождаться in-flight, сохранить checkpoint |
| Resume job | Продолжить из durable frontier |
| Graceful stop | Как pause, затем terminal `stopped` |
| Force stop | Отменить in-flight, записать cancelled attempt, освободить leases |
| Drain worker | Не брать новые operation, завершить текущую, остановиться |
| Restart worker | Drain old generation, запустить next generation |
| Disable worker | Desired state `disabled`, не участвует в reconciliation |
| Rotate connection | Drain, rotate конкретный VPNTE slot, health-check, resume |
| Retry operation | Создать новый attempt той же operation |
| Scale pool | Изменить desired workers, supervisor добавляет или drain-ит actors |

## 7. Что считать реальным worker

Реальный worker не обязан быть отдельным OS process.

Для этого проекта нагрузка преимущественно I/O-bound. На первом этапе оптимален долгоживущий `asyncio.Task` actor внутри backend process, если он выполняет worker contract:

- существует дольше одного request/batch;
- имеет stable identity и generation;
- имеет heartbeat;
- постоянно читает durable queue;
- владеет transport lease;
- принимает команды;
- сохраняет состояние независимо от UI;
- восстанавливается supervisor после рестарта;
- предоставляет одинаковый API независимо от runtime implementation.

Отдельные процессы сейчас не дают автоматического выигрыша:

- HTTP workload не упирается в GIL;
- Windows packaging и lifecycle станут сложнее;
- потребуется отдельный IPC/broker;
- VPNTE уже даёт нужную сетевую изоляцию по slots.

Нужно сразу определить interface `WorkerRuntime`:

~~~python
class WorkerRuntime(Protocol):
    async def start(self, spec: WorkerSpec) -> WorkerHandle: ...
    async def drain(self, worker_id: str) -> None: ...
    async def stop(self, worker_id: str, force: bool = False) -> None: ...
    async def restart(self, worker_id: str) -> WorkerHandle: ...
    async def snapshot(self) -> list[WorkerStatus]: ...
~~~

Первая реализация: `InProcessActorRuntime`.
Будущие реализации: `ProcessWorkerRuntime`, `RemoteWorkerRuntime`.

### 7.1 Почему не Celery

Celery умеет worker inspection, remote control и events, но для текущего Desktop-приложения это плохой базовый выбор:

- Celery официально не поддерживает Windows начиная с 4.x;
- потребуется Redis/RabbitMQ как обязательная внешняя инфраструктура;
- управление VPNTE slots и source cursors всё равно придётся реализовывать отдельно;
- текущий масштаб 10 workers и 10k карточек не требует отдельного distributed framework.

Это не запрещает поздний переход на broker-backed runtime через уже выделенные interfaces.

## 8. Оптимальная стратегия поиска

### 8.1 Вердикт по текущему подходу

Текущий подход неоптимален:

- делает десятки заведомо повторных mobile requests;
- получает по 10 occurrences, хотя доказанный web source возвращает 24;
- не использует дешёвую карту рынка перед сбором;
- не проверяет cursor contract;
- не имеет partition strategy для cap 1000;
- не сохраняет frontier;
- запускает AI внутри сетевого request;
- не отделяет worker от transport;
- не может управляться из UI.

### 8.2 Двухфазная модель

#### Phase A: market map

Цель — дешёво понять структуру, не скачивая тысячи карточек.

Последовательность:

1. `catalogRubrics`.
2. `catalogCategories(rubricId)`.
3. `catalogFilters(categoryId)` с camelCase.
4. `categoryAttributes(category_id)` для child/leaf.
5. `catalogMainv2` как seed hints.
6. Mobile `/kworks` page 1 как first-page cross-check.
7. Resolve и validate canonical web alias через `activeCategoryId`.

Результат Phase A:

- aggregate scope volume;
- known classifier/attribute partitions;
- canonical aliases;
- source capabilities;
- expected shard sizes;
- scan plan и budget estimate.

#### Phase B: card collection

Основной путь:

1. Initial web batch по canonical alias.
2. Извлечь cards из `viewData.kworks.posts.data`.
3. Зафиксировать фактический batch size.
4. Сформировать cursor из ID, уже увиденных в этом shard.
5. Запросить следующую пачку с `excludeIds=<csv>` и `onePage=1`.
6. Принять результат только после contract validation.
7. Сохранить batch, observations, cursor и metrics одной транзакцией.
8. Создать следующую operation для этого shard только после commit предыдущей.

Mobile source:

- используется для counts/classifiers/first page;
- `page>1` запрещён capability flag, пока live contract test не станет зелёным;
- при cursor mismatch source получает state `contract_violation`, а не бесконечный retry.

### 8.3 Source adapter contract

~~~python
class MarketSource(Protocol):
    name: str
    capabilities: SourceCapabilities

    async def map_scope(
        self,
        scope: MarketScope,
        transport: TransportLease,
    ) -> ScopeMap: ...

    async def fetch_batch(
        self,
        shard: ShardSpec,
        cursor: SourceCursor | None,
        transport: TransportLease,
    ) -> BatchResult: ...

    def validate_batch(
        self,
        request: BatchRequest,
        response: BatchResult,
        previous: BatchState | None,
    ) -> ContractVerdict: ...

    def normalize(self, raw_card: dict) -> NormalizedListing: ...
~~~

`BatchResult` обязан содержать:

- requested cursor;
- reported cursor;
- actual item count;
- cards;
- sorted-ID fingerprint;
- source total/total_found;
- next cursor;
- protection status;
- raw response reference;
- timing and bytes.

### 8.4 Canonical alias registry

Нужен durable `CategoryAliasRegistry`:

- category ID;
- canonical path/alias;
- source URL;
- validation status;
- `activeCategoryId` from web response;
- last validated timestamp;
- last protection state;
- schema version.

Alias считается валидным только если:

- response `success=true`;
- `activeCategoryId` совпадает с selected category;
- card shape распознан;
- первая пачка содержит IDs или честно сообщает empty.

Nested paths нельзя ограничивать текущим regex одного slug. Registry должен хранить нормализованный canonical path, но запрещать arbitrary host/path traversal.

### 8.5 Partitioning до 10 000

Один `website-repair` stream показывает `total=1000` при `total_found около 13k`. Поэтому нужен partition planner.

Порядок доверия partitions:

1. Classification attribute values из `categoryAttributes`.
2. Classifier IDs, подтверждённые counts.
3. Canonical child aliases.
4. Multi-select attribute combinations только после проверки semantics.
5. Price ranges только после overlap/boundary tests.
6. Seller level/activity filters только как sampling dimensions, пока не доказана исчерпывающая разбивка.

Каждый partition проходит тест:

- filter реально меняет выдачу;
- count согласуется с aggregate API в допустимом диапазоне;
- две соседние partitions имеют измеримый overlap;
- boundary semantics понятны;
- global dedupe компенсирует overlap;
- partition не создаёт blind spot.

Coverage нельзя считать суммой partition counts, если partitions пересекаются.

### 8.6 Contract gates

Операция не принимается автоматически.

| Gate | Условие ошибки |
|---|---|
| Cursor match | reported cursor противоречит request |
| Fingerprint | fingerprint повторился без ожидаемого overlap |
| Novelty | `new_unique=0` при непустой response |
| Category identity | `activeCategoryId` не совпадает |
| Shape | cards не найдены в известной schema |
| Batch size | request limit не совпал с actual; использовать actual |
| Protection | 403/429/challenge markers |
| Empty | empty подтверждён без protection/parse error |

Рекомендуемая политика stalled stream:

- первый zero-novelty: retry после cooldown на том же transport;
- второй zero-novelty с тем же fingerprint: rotate/retry при разрешённой policy;
- повтор после независимого transport: `contract_violation` или `exhausted` только по source-specific rule;
- не планировать page 3, 4, 5 вслепую.

### 8.7 Защита и rate control

- global Kwork token bucket;
- отдельный limiter на source class;
- один request на VPNTE slot;
- adaptive concurrency;
- exponential backoff с jitter;
- honour `Retry-After`;
- 403/QRATOR переводит transport/source в quarantine;
- rotate только между batches и только после drain;
- после rotate обязательны `/instances` validation и lightweight health probe;
- CAPTCHA/manual verification создаёт blocked state и UI action, а не автоматический bypass.

## 9. End-to-end алгоритм job

1. UI отправляет create request.
2. API валидирует scope/profile и создаёт job в `preparing`.
3. API возвращает `202` менее чем за секунду.
4. Coordinator выполняет transport/session preflight.
5. Mapping operations собирают taxonomy, counts, filters и attributes.
6. Alias resolver находит и проверяет canonical web alias.
7. Planner создаёт initial shards и budgets.
8. Supervisor поднимает desired workers и закрепляет healthy transports.
9. Workers claim operations.
10. Каждый accepted batch фиксируется атомарно:
    - attempt;
    - raw page metadata;
    - listings;
    - observations;
    - shard cursor;
    - counters;
    - events;
    - checkpoint revision.
11. Planner после каждого batch решает:
    - продолжить shard;
    - открыть новый partition;
    - снизить/повысить concurrency;
    - остановиться по target/budget/protection.
12. После collection запускается selective enrichment.
13. MetricsProjector строит deterministic analytics.
14. AI получает aggregate map и stratified evidence отдельно.
15. Exporter создаёт итоговые artifacts.
16. Job переходит в `completed`.
17. Команда «Углубить» увеличивает target/budget той же job и переводит её обратно в `running`.

## 10. Execution profiles

| Профиль | Предлагаемый target | Стратегия | AI label |
|---|---:|---|---|
| Рабочий | 60 unique | Mapping всей scope, стратифицированные initial batches | Предварительный вывод |
| Глубокий | 500 unique | Resume той же job, больше shards и continuation | Устойчивее, но sample-based |
| Расширенный | 2 000 unique | Adaptive partitions, selective enrichment | Расширенная выборка |
| Полный/пользовательский | до 10 000 unique | Explicit budget, soak-safe collection | Вывод по фактически собранным данным |

Worker count не должен жёстко следовать profile.

Рекомендуемая policy:

- стартовать с 2 workers;
- повышать на 1 после стабильного окна;
- не превышать число healthy transports;
- снижать при 403/429/timeouts/низкой novelty;
- не запускать 10 workers автоматически только потому, что VPNTE имеет 10 slots.

## 11. VPNTE multi-slot integration

### 11.1 Текущий gap

PSR wrapper сейчас использует только default:

- `/status`;
- `/start`;
- `/rotate`;
- один env port/profile.

Файл: [vpnte_proxy.py](C:/psr/src/utils/vpnte_proxy.py:154).

Сам VPNTE уже поддерживает:

- `GET /instances`;
- `GET /status?slot=N`;
- `POST /start?slot=N`;
- `POST /rotate?slot=N`;
- `POST /stop?slot=N`;
- 10 slots;
- default ports 17990-17999;
- profile, country, PID и startedAt для каждого instance.

Источник: [externalProxy.ts](C:/Users/Redmi/CascadeProjects/vpn/vpn-tunnel-enforcer/src/main/externalProxy.ts:680).

### 11.2 Новый PSR client contract

~~~python
class VpnteTransportProvider:
    def instances(self) -> list[VpnteInstance]: ...
    def status(self, slot: int) -> VpnteInstance: ...
    def start(self, slot: int, country: str | None = None, profile_id: str | None = None) -> VpnteInstance: ...
    def rotate(self, slot: int, country: str | None = None) -> VpnteInstance: ...
    def stop(self, slot: int) -> VpnteInstance: ...
    def health(self, slot: int) -> TransportHealth: ...
~~~

### 11.3 Transport lifecycle

1. Supervisor выбирает free slot.
2. TransportManager читает `/instances`.
3. Если slot stopped, вызывает `start(slot)`.
4. Проверяет returned `proxyUrl`.
5. Проверяет local listener.
6. Делает lightweight Kwork probe.
7. Создаёт exclusive lease.
8. Worker получает transport snapshot.
9. При rotate worker drain-ится.
10. После rotate transport generation увеличивается.
11. Worker продолжает только после зелёного health.

### 11.4 Network policy

Job config:

- `direct_only`;
- `vpnte_only`;
- `prefer_vpnte`;
- `explicit_pool`.

Фактический fallback всегда создаёт event и меняет visible transport route. Silent fallback запрещён.

### 11.5 Что показывать в UI

Пример route:

`worker-03 -> VPNTE slot 3 -> 127.0.0.1:17992 -> profile nl-vless-2 -> Netherlands -> kwork.ru/catalog_kworks_filters/website-repair`

Показывать:

- slot;
- proxy port;
- profile name/id;
- country;
- PID;
- startedAt;
- health;
- target host/endpoint;
- current session source;
- transport generation;
- last rotate reason.

Не показывать:

- control token;
- cookies;
- passwords;
- proxy credentials;
- Authorization headers.

## 12. Durable storage

### 12.1 Выбор

Для локального Windows Desktop достаточно отдельной SQLite database:

`data/kwork_market_jobs.db`

Настройки:

- `PRAGMA journal_mode=WAL`;
- `PRAGMA synchronous=NORMAL` либо `FULL` для более строгой durability;
- `PRAGMA busy_timeout`;
- короткие transactions;
- индексы на queue/state/lease/event cursor;
- DB calls через dedicated repository и `asyncio.to_thread` либо single DB writer actor.

10 workers и 10 000 listings не требуют Redis как обязательной зависимости.

### 12.2 Таблицы

#### `market_jobs`

- id;
- scope fields;
- profile;
- target;
- budgets;
- desired workers;
- state;
- desired state;
- phase;
- revision;
- counters;
- timestamps;
- last error.

#### `market_shards`

- id/job;
- source;
- alias;
- filters JSON;
- expected count;
- state;
- priority;
- cursor JSON;
- fingerprint;
- counters;
- cooldown;
- timestamps.

#### `market_operations`

- id/job/shard;
- kind;
- state;
- priority;
- idempotency key;
- not-before;
- lease owner/deadline;
- current attempt;
- timestamps.

#### `market_operation_attempts`

- operation;
- worker/transport;
- requested/reported cursors;
- redacted request;
- response metadata;
- counts/fingerprint;
- error/protection;
- raw reference;
- timing.

#### `market_workers`

- worker ID/generation;
- desired/actual state;
- runtime kind;
- transport;
- current operation;
- heartbeat;
- counters;
- last error.

#### `market_transports`

- transport ID/kind;
- VPNTE slot;
- proxy URL;
- profile/country/PID;
- health;
- generation;
- lease owner;
- quarantine.

#### `market_listings`

- job ID/listing key;
- normalized searchable columns;
- canonical JSON;
- first/last seen;
- observation count.

#### `market_listing_observations`

- listing;
- attempt;
- source/shard;
- position;
- cursor provenance;
- raw reference;
- observed timestamp.

#### `market_events`

- job ID;
- sequence;
- type;
- payload;
- emitted timestamp.

#### `market_checkpoints`

- job ID/revision;
- phase;
- frontier JSON;
- metrics JSON;
- created timestamp.

#### `market_worker_commands`

- command ID;
- target worker/job;
- type;
- payload;
- state;
- created/acked/completed timestamps;
- error.

### 12.3 Raw response storage

Не хранить многосоткилобайтные raw web pages в WebSocket или localStorage.

Предлагаемый layout:

~~~text
data/kwork_market_jobs/
  {job_id}/
    raw/
      {operation_id}-{attempt}.json.gz
    exports/
      summary.json
      listings.jsonl.gz
      observations.jsonl.gz
~~~

DB хранит hash, size, schema version и path.

### 12.4 Transaction boundary

Accepted batch commit должен быть одной transaction:

1. завершить attempt;
2. upsert normalized listings;
3. append observations;
4. update shard cursor;
5. update job counters;
6. enqueue next operation;
7. append events;
8. increment revision/checkpoint.

При crash до commit operation lease истекает и batch выполняется снова.
При crash после commit idempotency key не позволяет принять его второй раз.

## 13. REST API

### 13.1 Jobs

| Method | Endpoint | Назначение |
|---|---|---|
| POST | `/api/kwork/market/jobs` | Создать job, вернуть 202 |
| GET | `/api/kwork/market/jobs` | Список/history |
| GET | `/api/kwork/market/jobs/{job_id}` | Полный snapshot |
| PATCH | `/api/kwork/market/jobs/{job_id}` | Extend target/budget/profile |
| POST | `.../{job_id}/pause` | Pause |
| POST | `.../{job_id}/resume` | Resume |
| POST | `.../{job_id}/stop` | Graceful/force stop |
| PATCH | `.../{job_id}/worker-pool` | Desired worker count |

### 13.2 Operational views

| Method | Endpoint |
|---|---|
| GET | `.../{job_id}/workers` |
| GET | `.../{job_id}/transports` |
| GET | `.../{job_id}/shards?cursor=&limit=` |
| GET | `.../{job_id}/operations?cursor=&limit=&state=` |
| GET | `.../{job_id}/listings?cursor=&limit=&shard=` |
| GET | `.../{job_id}/events?after_seq=&limit=` |
| GET | `.../{job_id}/results` |

### 13.3 Worker control

| Method | Endpoint |
|---|---|
| POST | `.../workers/{worker_id}/drain` |
| POST | `.../workers/{worker_id}/restart` |
| POST | `.../workers/{worker_id}/disable` |
| POST | `.../workers/{worker_id}/rotate` |
| POST | `.../workers/{worker_id}/reconnect` |
| POST | `.../operations/{operation_id}/retry` |

### 13.4 Create request

~~~json
{
  "scope": {
    "category_id": 38,
    "category_name": "Доработка и настройка сайта",
    "classifier_id": null
  },
  "profile": "working",
  "target_unique_cards": 60,
  "desired_workers": 2,
  "network_policy": "prefer_vpnte",
  "source_policy": "validated_only",
  "include_ai": true
}
~~~

Response:

~~~json
{
  "job_id": "job_01...",
  "state": "preparing",
  "revision": 1,
  "status_url": "/api/kwork/market/jobs/job_01...",
  "events_url": "/ws/kwork-market/jobs/job_01..."
}
~~~

## 14. Event stream

Endpoint:

`/ws/kwork-market/jobs/{job_id}?after_seq=N`

Envelope:

~~~json
{
  "schema_version": 1,
  "seq": 1842,
  "job_id": "job_01...",
  "revision": 315,
  "type": "operation.completed",
  "emitted_at": "2026-07-11T12:00:00Z",
  "worker_id": "worker-03",
  "operation_id": "op_01...",
  "payload": {
    "received": 24,
    "new_unique": 24,
    "duplicates": 0
  }
}
~~~

Обязательные event types:

- `job.snapshot`;
- `job.state_changed`;
- `job.phase_changed`;
- `job.metrics`;
- `worker.registered`;
- `worker.state_changed`;
- `worker.heartbeat`;
- `transport.state_changed`;
- `operation.queued`;
- `operation.started`;
- `operation.completed`;
- `operation.failed`;
- `operation.contract_violation`;
- `shard.progress`;
- `checkpoint.saved`;
- `warning`;
- `result.ready`.

Event replay:

1. Client сохраняет last `seq`.
2. После reconnect передаёт `after_seq`.
3. Backend replay-ит durable events.
4. Если retention gap слишком велик, backend отправляет `resync_required`.
5. Client получает свежий job snapshot и продолжает с нового sequence.

Existing `useWebSocket` можно использовать как основу, но нужен отдельный `useMarketJobStream` с connection states:

- `connecting`;
- `live`;
- `disconnected`;
- `resyncing`;
- `stale`.

## 15. UI control plane

### 15.1 Navigation

Предлагаемые routes:

- `/kwork-market` — выбор scope, jobs/history;
- `/kwork-market/jobs/{job_id}` — active job control plane.

Не встраивать весь monitor в текущий компонент на тысячи строк.

### 15.2 Active job layout

Верхняя control bar:

- state и phase;
- profile;
- elapsed time;
- checkpoint age;
- WebSocket state;
- target;
- desired/ready/busy workers;
- Pause/Resume;
- Graceful Stop;
- menu с Force Stop;
- worker count stepper.

Tabs:

1. Обзор.
2. Воркеры.
3. Запросы.
4. Срезы.
5. Данные.
6. Результаты.
7. События.

### 15.3 Overview metrics

Показывать одновременно:

- unique cards: `2 416 / 10 000`;
- card occurrences;
- shards complete/active/blocked;
- operations complete/queued/retry;
- workers busy/ready/unhealthy;
- unique cards per minute;
- duplicate rate;
- error/protection rate;
- latency P50/P95;
- checkpoint age.

Не показывать один искусственный «процент готовности», если denominator неизвестен.

### 15.4 Workers table

Columns:

`State | Worker | Connection | Current operation | Elapsed | New/Dup | Rate | Heartbeat | Actions`

Пример row:

`busy | worker-03#2 | slot 3 / NL / 17992 | web: website-repair / cursor 288 IDs | 1.2s | 24/0 | 18/min | 0.6s | ...`

Worker detail panel:

- identity и generation;
- runtime task/process ID;
- actual/desired state;
- transport route;
- VPNTE slot/profile/country/PID;
- session source;
- target host/endpoint;
- current job/shard/operation;
- requested/reported cursor;
- attempt timeline;
- totals;
- latency P50/P95;
- recent errors;
- backoff/quarantine;
- commands: Drain, Restart, Rotate, Reconnect, Disable.

### 15.5 Operations table

Columns:

- time;
- operation;
- worker;
- transport;
- source;
- alias/shard;
- requested cursor;
- reported cursor;
- received;
- new;
- duplicates;
- status;
- latency;
- error/retry.

Filters:

- state;
- worker;
- source;
- shard;
- contract violations;
- protection;
- duplicates.

### 15.6 Shards view

Показывать:

- shard/filter;
- declared count;
- source;
- cursor size;
- occurrences;
- global new unique;
- novelty rate;
- duplicate rate;
- last fingerprint;
- state;
- cooldown;
- next action.

### 15.7 Data view

- server-side pagination;
- filtering by shard/source/seller/price;
- grouping by slice;
- virtualized table/list;
- no 10 000 DOM nodes;
- card provenance drawer;
- raw observation count.

Это исправляет текущую иллюзию, когда первые визуальные карточки принадлежат одному slice.

### 15.8 Results view

Перенести туда:

- aggregate map;
- price distribution;
- seller repetition;
- niches;
- positioning hypotheses;
- Market Assistant.

Structured AI renderer обязан поддерживать:

- string;
- list;
- object;
- nested evidence rows.

Нельзя делать `String(object)`, потому что это создаёт `[object Object]`.

### 15.9 Truthful labels

| Сейчас | Должно быть |
|---|---|
| «охват срезов 96,8%» | «объём доступных API-срезов: 12 618» |
| «концентрация продавцов» | «повторяемость продавцов в наблюдаемой выборке» |
| min/max как рынок | P10/P50/P90, count with price, cheap share |
| «цель 500 не достигнута» в рабочем проходе | «рабочая выборка завершена; углубить анализ» |
| AI вывод без статуса | «предварительная гипотеза по N карточкам» |
| pool size как workers | «transport slots», пока actors не реализованы |

## 16. Метрики и аналитическая корректность

### 16.1 Четыре независимые группы метрик

#### Aggregate scope

- reported category volume;
- aggregate volume of selected slices;
- available filter dimensions;
- declared shard counts.

#### Observed cards

- card occurrences;
- global unique cards;
- unique sellers;
- listings with valid price;
- first/last observation.

#### Continuation health

- active/exhausted/blocked shards;
- novelty rate;
- duplicate rate;
- cursor size;
- repeated fingerprints;
- contract violations.

#### System health

- worker state;
- transport health;
- throughput;
- latency;
- error/protection rate;
- queue depth;
- checkpoint age.

### 16.2 Price metrics

Обязательно:

- `price_count`;
- P10;
- P25;
- P50;
- P75;
- P90;
- min/max как secondary outlier context;
- configurable cheap threshold;
- cheap share;
- missing-price share;
- distributions by shard.

### 16.3 Seller metrics

Разрешено:

- unique sellers;
- cards per seller in observed data;
- repeat share in observed data;
- top repeated sellers как diagnostic.

Нельзя называть это market concentration без полноценной methodology и достаточного unbiased sample.

### 16.4 AI evidence

AI получает два раздельных блока:

1. Deterministic aggregate map.
2. Stratified card evidence.

Для 10k не отправлять все raw cards:

- deterministic aggregates по всем cards;
- stratified sample по shards, price bands и seller frequency;
- отдельный evidence manifest;
- exact included/raw counts;
- preliminary/extended status;
- ссылки на listing IDs/observations.

## 17. Масштаб до 10 000

### 17.1 Capacity model

Live actual batch для `website-repair`: 24 cards.

Теоретический минимум при нулевых duplicates:

`ceil(10 000 / 24) = 417 accepted batches`

Практически потребуется больше из-за:

- overlaps между shards;
- protection/retries;
- stale responses;
- partial batches;
- cap 1000 на stream;
- filter partitions.

Планирование должно использовать:

`estimated_operations = ceil(remaining_target / rolling_p50_new_unique_per_accepted_batch)`

Нельзя рассчитывать budget из requested `pageSize`.

### 17.2 Adaptive concurrency

Начало: 2 workers.

Scale up на 1, если за стабильное окно:

- все transports healthy;
- нет 403/429;
- timeout rate ниже threshold;
- novelty выше threshold;
- checkpoint latency нормальна.

Scale down, если:

- protection signal;
- растёт retry rate;
- падает novelty;
- transport health degraded;
- DB/event backlog.

### 17.3 Memory и UI

Backend:

- batch processing;
- bounded in-memory queues;
- compressed raw pages;
- DB pagination;
- event coalescing раз в 500-1000 ms;
- не публиковать event на каждую карточку.

Frontend:

- хранить только job summary, workers, active operations и bounded event ring;
- listings получать страницами;
- localStorage содержит только active job ID, filters и selected tab;
- backend остаётся source of truth.

### 17.4 Resume

Crash recovery:

1. Lifespan supervisor загружает active jobs.
2. Expired leases возвращаются в queue.
3. Old workers отмечаются crashed/stale.
4. Desired workers создаются с new generation.
5. Shard cursors берутся из latest committed checkpoint.
6. Replayed attempt не увеличивает unique count повторно.

## 18. Предлагаемая структура файлов

### 18.1 Backend

~~~text
src/platforms/kwork_supply/
  __init__.py
  models.py
  repository.py
  events.py
  coordinator.py
  supervisor.py
  worker.py
  planner.py
  metrics.py
  analyzer.py
  exporter.py
  transports/
    base.py
    vpnte.py
  sources/
    base.py
    taxonomy.py
    mobile_kworks.py
    web_catalog.py
    enrichment.py

src/api/routes/kwork_market_jobs.py
~~~

`kwork_market_supply.py` оставить временным compatibility adapter, затем удалить после cutover.

### 18.2 Frontend

~~~text
desktop/src/features/kwork-market/
  api.ts
  types.ts
  hooks/
    useMarketJobs.ts
    useMarketJobStream.ts
  state/
    jobReducer.ts
  components/
    JobToolbar.tsx
    JobMetrics.tsx
    WorkerTable.tsx
    WorkerDetailPanel.tsx
    OperationTable.tsx
    ShardProgressTable.tsx
    ListingsTable.tsx
    ResultsPanel.tsx
    EventTimeline.tsx

desktop/src/pages/KworkMarketWorkspace.tsx
desktop/src/pages/KworkMarketJob.tsx
~~~

### 18.3 Tests

~~~text
tests/unit/test_kwork_source_contracts.py
tests/unit/test_kwork_web_catalog_source.py
tests/unit/test_market_job_repository.py
tests/unit/test_market_job_coordinator.py
tests/unit/test_market_worker_supervisor.py
tests/unit/test_market_transport_manager.py
tests/unit/test_market_job_routes.py
tests/unit/test_market_event_replay.py

tests/integration/test_kwork_web_continuation_live.py
tests/integration/test_market_job_resume.py
tests/smoke/test_market_job_10k_synthetic.py
~~~

## 19. Этапы реализации

### Phase 0. Truth and contract gates

Изменения:

- перестать планировать mobile page > 1 без validation;
- добавить requested/reported cursor;
- добавить fingerprint/new/duplicate counters;
- исправить first-seen provenance;
- добавить live contract probe harness;
- временно называть текущий pool transport slots;
- исправить `[object Object]` и misleading UI labels.

Acceptance:

- mobile page mismatch автоматически останавливает stream;
- duplicate page видна в UI/result;
- рабочий проход корректно называется рабочим;
- aggregate volume не называется coverage.

### Phase 1. Source adapters и реальный continuation

Изменения:

- `SourceAdapter` interface;
- web card parser для `viewData.kworks.posts.data`;
- `excludeIds + onePage` cursor;
- canonical alias registry;
- category/filter/attribute mapping;
- raw batch artifacts;
- source-specific contract tests.

Acceptance:

- category 38 alias проверяется как `activeCategoryId=38`;
- минимум 3 последовательных accepted web batches;
- минимум 72 unique и нулевой overlap в контрольном smoke;
- requested pageSize не влияет на expected count;
- 403 не считается empty/exhausted.

### Phase 2. Durable jobs и worker actors

Изменения:

- SQLite schema/WAL;
- coordinator;
- supervisor в FastAPI lifespan;
- stable workers;
- operation leasing;
- checkpoint после batch;
- pause/resume/stop;
- crash recovery;
- event store.

Acceptance:

- create возвращает 202 быстро;
- backend restart продолжает job;
- pause не создаёт новые leases;
- drain одного worker не останавливает остальных;
- replay не дублирует unique cards.

### Phase 3. VPNTE multi-slot

Изменения:

- slot-aware PSR client;
- `/instances` polling;
- start/rotate/stop specific slot;
- transport leases;
- health/quarantine;
- worker-to-transport route.

Acceptance:

- каждый worker видит свой slot/profile/port;
- два workers не используют один slot;
- rotate одного slot не затрагивает другие;
- stopped/unhealthy slot не получает operation;
- no silent fallback.

### Phase 4. Control-plane UI

Изменения:

- jobs routes/pages;
- replayable stream hook;
- worker table/detail;
- operation/shard/listing views;
- controls;
- truthful results renderer;
- server-side pagination.

Acceptance:

- UI восстанавливает active job после reload;
- виден heartbeat и current operation каждого worker;
- виден requested/reported cursor;
- WS disconnect и resync явно отображаются;
- 10k dataset не загружается целиком в DOM/localStorage.

### Phase 5. Deep and 10k scale

Изменения:

- partition planner;
- adaptive concurrency;
- global dedupe;
- selective enrichment;
- deterministic quantiles;
- stratified AI evidence;
- compressed exports;
- soak/fault tests.

Rollout gates:

1. 60 unique working run.
2. Resume до 500.
3. Resume до 2 000.
4. Controlled run до 10 000.

Каждый gate требует:

- нет stalled streams;
- checkpoint/resume работает;
- no unbounded memory growth;
- protection rate контролируется;
- metrics совпадают с DB;
- UI остаётся responsive.

### Phase 6. Cutover

Изменения:

- новый UI становится default;
- старый endpoint создаёт job compatibility mode либо возвращает deprecation;
- legacy snapshots доступны read-only;
- старый scanner удаляется после parity window.

## 20. Test matrix

| Сценарий | Ожидаемый результат |
|---|---|
| Mobile page 2 возвращает page 1 | Contract violation, page 3 не планируется |
| Web page 1/2 переставляет те же IDs | 0 new, stalled detection |
| Web exclude cursor возвращает новые IDs | Accepted batch, cursor/checkpoint updated |
| Requested 50, actual 24 | Metrics/budget используют 24 |
| 403 QRATOR | Blocked/quarantine, не exhausted |
| 429 + Retry-After | Retry wait с указанной задержкой |
| Duplicate batch after crash | Idempotent commit |
| Worker crash mid-request | Lease expires, attempt requeued |
| Pause job | In-flight finishes, new leases отсутствуют |
| Drain worker | Остальные workers продолжают |
| Rotate slot 3 | Только worker на slot 3 drain/reconnect |
| Backend restart | Active job resumes from committed cursor |
| WS gap | Replay by seq или full resync |
| 10k synthetic with duplicates | Exactly 10k global unique, bounded memory |
| AI object field | Structured UI, no `[object Object]` |

## 21. Risks and mitigations

| Риск | Mitigation |
|---|---|
| Hidden web contract изменится | Source adapter versioning, schema detection, contract probes |
| QRATOR block | Adaptive concurrency, quarantine, explicit blocked state |
| Alias drift | Validate `activeCategoryId`, registry TTL |
| Stream cap 1000 | Classifier/attribute partitions |
| Filters пересекаются | Global dedupe, overlap metrics, no false coverage |
| VPNTE slot stopped | `/instances` health, explicit start, no assumed port |
| SQLite writer contention | WAL, short transactions, indexes, DB writer actor |
| Event flood | Batch events, coalesced metrics, retention |
| LLM token growth | Deterministic aggregates + stratified sample |
| UI stale snapshot | Revision/seq, live state, stale badge |
| Hidden retry duplication | At-least-once + idempotency keys |
| Ten workers trigger protection | Start at 2, adaptive scale |

## 22. Решения, которые не следует принимать

- Не повышать `max_requests` до тысяч в текущем scanner.
- Не называть массив proxy clients workers.
- Не запускать 10 workers автоматически.
- Не использовать `page=N` без response cursor validation.
- Не верить requested `pageSize`.
- Не считать 403 пустой выдачей.
- Не суммировать пересекающиеся filter counts как market coverage.
- Не хранить 10k raw cards в terminal HTTP response.
- Не хранить job result в localStorage.
- Не enrich-ить seller/details для всех 10k карточек.
- Не rotate-ить VPN на каждом item/request.
- Не добавлять Celery только ради слова «worker».

## 23. Definition of Done

Архитектура считается реализованной, когда:

1. Start создаёт durable job и возвращает ID менее чем за секунду.
2. У каждого worker есть stable ID, generation, heartbeat и управляемый lifecycle.
3. UI показывает полный connection route и current operation.
4. Каждый operation сохраняет requested и reported cursor.
5. Mobile pagination defect автоматически обнаруживается.
6. Web continuation доказан contract tests.
7. Pause/resume/drain/restart/rotate работают из UI.
8. Backend restart не теряет прогресс.
9. Рабочий проход можно продолжить глубоким в той же job.
10. Aggregate volume и observed cards отображаются раздельно.
11. Price UI показывает P10/P50/P90 и price count.
12. Seller repetition не называется concentration.
13. AI hypotheses маркируются объёмом evidence.
14. 10k synthetic run проходит с bounded memory.
15. Controlled live gates 500 -> 2 000 -> 10 000 проходят последовательно.

## 24. Рекомендуемый порядок первого PR

Первый PR не должен сразу строить весь UI.

Минимальный правильный порядок:

1. Добавить source contract types.
2. Добавить web card parser и `excludeIds + onePage`.
3. Добавить cursor/fingerprint/novelty validation.
4. Добавить tests, которые воспроизводят current mobile defect.
5. Доказать 3+ последовательных web batches.
6. Только после этого добавлять job database и workers.

Иначе есть риск построить красивую управляемую worker-систему поверх нерабочего card source.

## Appendix A. Основные локальные evidence points

| Тема | Ссылка |
|---|---|
| Current scanner | [kwork_market_supply.py](C:/psr/src/platforms/kwork_market_supply.py:504) |
| Current sync route | [kwork.py](C:/psr/src/api/routes/kwork.py:768) |
| Current UI launch | [KworkMarket.tsx](C:/psr/desktop/src/pages/KworkMarket.tsx:1396) |
| Current API type | [api.ts](C:/psr/desktop/src/lib/api.ts:548) |
| Web helper | [kwork_market.py](C:/psr/src/platforms/kwork_market.py:1065) |
| Mobile get_kworks | [kwork_market.py](C:/psr/src/platforms/kwork_market.py:1230) |
| Existing WebSocket | [ws.py](C:/psr/src/api/ws.py:15) |
| Existing WS hook | [useWebSocket.ts](C:/psr/desktop/src/hooks/useWebSocket.ts:5) |
| Existing Queue master-detail | [Queue.tsx](C:/psr/desktop/src/pages/Queue.tsx:429) |
| VPNTE PSR wrapper | [vpnte_proxy.py](C:/psr/src/utils/vpnte_proxy.py:154) |
| VPNTE slot API | [externalProxy.ts](C:/Users/Redmi/CascadeProjects/vpn/vpn-tunnel-enforcer/src/main/externalProxy.ts:680) |
| JS continuation contract | [kwork_js_priority_endpoint_context_2026-07-08T145218Z.json](C:/psr/docs/kwork_js_priority_endpoint_context_2026-07-08T145218Z.json:902) |
| Failed supply snapshot | [kwork_supply_20260710T192859Z.json](C:/psr/docs/kwork_market_supply_snapshots/kwork_supply_20260710T192859Z.json:30) |

## Appendix B. Внешние архитектурные источники

Поиск выполнялся через обычный Tavily Search, без Tavily Research.

- [Celery FAQ: Windows официально не поддерживается с Celery 4.x](https://docs.celeryq.dev/en/main/faq.html)
- [Celery workers и remote control как reference model](https://docs.celeryq.dev/en/stable/userguide/workers.html)
- [SQLite WAL и concurrency](https://sqlite.org/wal.html)
- [SQLite isolation](https://sqlite.org/isolation.html)
- [Redis Streams consumer groups и claim/recovery как future broker reference](https://redis.io/docs/latest/develop/data-types/streams)
- [PostgreSQL `SKIP LOCKED` для queue-like tables как future multi-host option](https://www.postgresql.org/docs/current/sql-select.html)

## Appendix C. Future scale-out boundary

Локальная реализация должна зависеть от interfaces:

- `OperationBroker`;
- `EventStore`;
- `WorkerRuntime`;
- `TransportProvider`;
- `ArtifactStore`.

Первая конфигурация:

- `SqliteOperationBroker`;
- `SqliteEventStore`;
- `InProcessActorRuntime`;
- `VpnteTransportProvider`;
- `LocalArtifactStore`.

Будущая multi-host конфигурация без изменения UI:

- Redis Streams или PostgreSQL broker;
- remote worker runtime;
- shared object/artifact storage;
- тот же REST/WebSocket schema.

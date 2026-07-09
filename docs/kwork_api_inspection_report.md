# Kwork API Inspection Report

Implementation backlog:

- `docs/kwork_market_api_integration_backlog.md`

Дата: 2026-07-08

## Цель

Найти и проверить API Kwork, полезные для быстрого Kwork Market, поиска заказов, анализа рынка, фильтров, форм публикации и конкурентного анализа. Инспект велся через `autopentest_ai` engagement `kwork-api-inspection`, Tavily crawl/map, авторизованный Kwork API client, Session Hub cookies и разбор JS-бандлов Kwork.

Секреты не сохранялись: в документе нет пароля, token, cookie values или Authorization header.

## Методика и источники

- `autopentest_ai`: WSTG-INFO-06 для entry points, WSTG-INFO-10 для архитектуры, API technique guide, endpoint graph, priority queue.
- Tavily: map/crawl `https://kwork.ru`, `/categories/programming`, `/projects`.
- Direct HTTP: `curl`/Python requests к `kwork.ru` и `api.kwork.ru`.
- Credentialed mobile API: Python `kwork.Kwork`, логин/пароль из окружения/локальной конфигурации.
- Session Hub: `SESSION_HUB_URL` вернул `status=ok`, 12 cookies, включая `csrf_user_token`, `slrememberme`, `userId`.
- JS bundles: `header_*.js`, `general_*.js`, `projects-list-fcp_*.js`, `projects-list-dcl_*.js`, `allAttributes.min.js`.
- Local package map: `kwork/openapi_mixin.py` дал 106 релевантных mobile API методов.

## Архитектура

- `https://kwork.ru`: web app, `Server: QRATOR`, HTML + same-origin `/api/...`.
- `https://api.kwork.ru`: mobile JSON API, `Server: QRATOR`, без mobile Authorization header отвечает `401 Unauthorized` и `WWW-Authenticate: Basic realm="Authentication Required!"`.
- `https://cdn-edge.kwork.ru`: static CDN, `Server: nginx`, `Access-Control-Allow-Origin: *`, cache headers.
- OpenAPI/Swagger docs paths проверены: на `kwork.ru` в основном `404`, на `api.kwork.ru` `401`.

## Авторизация

`api.kwork.ru` не работает от обычной web-cookie с `kwork.ru`. Нужен mobile API client:

- все mobile API запросы идут с библиотечным Authorization header;
- `use_token=True` endpoint-ы дополнительно получают `token` через `POST /signIn`;
- `POST /signIn` принимает `login`, `password`, опционально `phone_last`;
- web endpoints `kwork.ru/api/...` используют web cookies и иногда `csrf_user_token`.

## Подтвержденные основные API

### Market / конкуренты

`POST https://api.kwork.ru/categories`

- Auth: mobile basic header.
- Ответ: `success`, `response[]`.
- Проверка: 7 root categories, first item содержит `id`, `name`, `description`, `subcategories`.

`POST https://api.kwork.ru/kworks`

- Auth: mobile basic header.
- Параметры: `categoryId`, `classifierId`, `page`; `attribute[...]` принимается, но не всегда сужает выдачу.
- Ответ: `response.kworks_count`, `response.kworks[]`, `response.classifiers[]`, `response.paging`.
- Проверка `categoryId=41`: `kworks_count=38124`, returned 10, classifiers: `211 Парсеры`, `3587 Чат-боты`, `7352 Скрипты`, `3934090 Telegram Mini Apps`, `5548694 ИИ-агенты`.
- Проверка `categoryId=41&classifierId=3587`: `kworks_count=18261`, classifiers: `12410 Написание и доработка`, `12411 Готовые`.
- Проверка `categoryId=41&attribute[208]=3587`: count остался `38124`, то есть attribute не сузил выдачу как classifier.

Главное правило: для релевантных конкурентов использовать самый глубокий выбранный пункт как `classifierId`. Для multi-select делать capped fan-out по нескольким `classifierId`. Не полагаться на `attribute[...]` для `/kworks`.

`POST https://api.kwork.ru/categoryAttributes`

- Auth: mobile basic header.
- Параметр: `category_id`.
- Ответ: дерево атрибутов, first item содержит `id`, `category_id`, `children`, `allow_multiple`, `required`, `kworks_count`, `percent_usage` и др.

`POST https://api.kwork.ru/getKworkDetails`

- Auth: mobile basic header.
- Параметр: `id`.
- Ответ: `kwork_title`, `kwork_description`, `kwork_instructions`, `default_kwork_price`, `term`, `short_user_info`, `packages`, `portfolio_items`, `orders_in_queue_count`.
- Назначение: заменить HTML загрузку `share_url` для подробностей конкурента.

`POST https://api.kwork.ru/getKworkDetailsExtra`

- Auth: mobile basic header.
- Параметр: `id`.
- Ответ: `reviews_count`, `last_reviews`, `goodReviews`, `badReviews`, `kwork_ratings`, `similar_kworks`, `recommended_kworks`, `other_kworks`, `frequently_asked_questions_count`.
- Назначение: конкурентный анализ без HTML.

`POST https://api.kwork.ru/getKworkReviews`

- Auth: mobile basic header.
- Параметры: `kwork_id`, `page`.
- Ответ: `response[]`, `paging`; first review содержит `id`, `text`, `good`, `bad`, `answer`, `writer`, `time_added`.

`POST https://api.kwork.ru/getKworkAnswers`

- Auth: mobile basic header.
- Параметр: `id`.
- Ответ: FAQ list, поля `id`, `question`, `answer`, `position`.

`POST https://api.kwork.ru/getKworkPortfolios`

- Auth: mobile basic header.
- Параметры: `id`, `page`.
- Ответ: `response[]`, `paging`; для проверенного кворка список был пустой.

`POST https://api.kwork.ru/getKworkLinksTable`

- Auth: mobile basic header.
- Параметр: `id`.
- Ответ: `response[]`, `paging`; для проверенного кворка список был пустой.

### Спрос / заказы

`POST https://api.kwork.ru/projects`

- Auth: `token+basic`.
- Параметры: `categories`, `page`, `query`, `price_from`, `price_to`, `hiring_from`, `kworks_filter_from`, `kworks_filter_to`.
- Проверка `categories=all&page=1`: `response[]` length 12, top-level `paging`, `connects`, `fcmTokenLost`.
- First project fields: `id`, `title`, `description`, `price`, `possible_price_limit`, `offers`, `category_id`, `parent_category_id`, `time_left`, `user_id`, `username`, `user_projects_count`, `user_hired_percent`, `has_offer`.
- Назначение: основной быстрый источник заказов/спроса.

`POST https://api.kwork.ru/getWantsCount`

- Auth: `token+basic`.
- Параметры: `categories`, `query`, `price_from`, `price_to`, и другие фильтры.
- Проверка `categories=all`: `response.count=534`, `response.filters`.
- Назначение: быстрый счетчик спроса.

`POST https://api.kwork.ru/project`

- Auth: `token+basic`.
- Параметр: `id`.
- Проверка на want id `3213259`: ответ `success=true`, `response` dict с `id`, `title`, `description`, `price`, `possible_price_limit`, `offers`, `category_id`, `user_hired_percent`, `time_left`.
- Назначение: детальная карточка заказа.

`POST https://api.kwork.ru/want`

- Auth: `token+basic`.
- Параметр: `id`.
- Проверка на want id `3213259`: ответ `response[]` length 1, поля `id`, `title`, `description`, `price_limit`, `possible_price_limit`, `offers`, `orders`, `views`, `views_history`, `date_active`, `date_create`, `date_expire`.
- Назначение: альтернативная детальная карточка заказа, с просмотрами и historical fields.

`POST https://api.kwork.ru/wantsStatusList`

- Auth: `token+basic`.
- Ответ: список wants по статусам; на проверке length 0.

`POST https://api.kwork.ru/myWants`

- Auth: `token+basic`.
- Ответ: `response[]`, `paging`; на проверке length 0.

`POST https://api.kwork.ru/offers`

- Auth: `token+basic`.
- Ответ: `response[]`, `paging`, `connects`; на проверке offers length 0, `connects.active_connects=30`.

`POST https://api.kwork.ru/actor`

- Auth: `token+basic`.
- Ответ: текущий аккаунт, включая `id`, `username`, `email`, `free_amount`, `total_amount`, `kworks[]`, `kworks_count`, `offers_count`, `wants_count`, `archived_wants_count`, `rating`, `reviews`, `worker_status`, `notify_unread_count`.
- Назначение: аккаунт, лимиты, свои кворки, базовая аналитика.

`POST https://api.kwork.ru/getActorInfo`

- Auth: `token+basic`.
- Ответ: компактные данные аккаунта: `id`, `username`, `verified`, `kworks_count`, `offers_count`, `hidden_kworks_count`, `favourite_kworks_count`, `total_amount`, `worker_status`.

`POST https://api.kwork.ru/kworksStatusList`

- Auth: `token+basic`.
- Ответ: 8 status tabs, each with `id`, `name`, `kworks_count`, `kworks`.
- Назначение: свои кворки по статусам.

`POST https://api.kwork.ru/viewedCatalogKworks`

- Auth: `token+basic`.
- Ответ: viewed kworks list + paging; на проверке 2 записи.

### Каталог / поиск / аналитика

`POST https://api.kwork.ru/catalogFilters`

- Auth: mobile basic header.
- Параметр: `categoryId`.
- Ответ: `response.filters[]`, `response.groups`, `response.kworks_count`.
- First filter fields: `query_key`, `type`, `name`, `values`, `min_value`, `max_value`, `unit`, `mobile_group_id`.
- Назначение: фильтры каталога, полезно для UI и точных параметров.

`POST https://api.kwork.ru/searchKworksCatalogQuery`

- Auth: mobile basic header.
- Параметр: `query`.
- Проверка `query=telegram`: 9 suggestions, fields `suggestion`, `excerpt`.
- Назначение: подсказки/семантика поиска кворков.

`POST https://api.kwork.ru/positiveReviewsCount`

- Auth: mobile basic header.
- Параметр в проверке: `category_id=41`.
- Ответ: dict по thresholds `1`, `5`, `10`, `20`, `50`, `100`.
- Назначение: фильтр/аналитика по положительным отзывам.

`POST https://api.kwork.ru/userByUsername`

- Auth: mobile basic header.
- Параметр: `username`.
- Ответ: профиль пользователя: `id`, `username`, `rating`, `reviews_count`, `good_reviews`, `bad_reviews`, `kworks_count`, `kworks`, `portfolio_list`, `skills`, `order_done_persent`, `order_done_intime_persent`, `order_done_repeat_persent`, `is_allow_custom_request`, `custom_request_min_budget`.
- Назначение: анализ заказчиков/продавцов.

`POST https://api.kwork.ru/userSearch`

- Auth: mobile basic header.
- Параметр: `query`.
- Ответ: `response[]`, `paging`; на проверке `grandlers` вернул 1 пользователя.

### Web API формы/цен

`GET https://kwork.ru/api/attribute/loadclassification`

- Auth: web session; публично тоже отдавал `200`.
- Параметры: `categoryId`, `lang`, опционально `attributeId`, `value`.
- Ответ: `success=true`, `html`, иногда `attributeResolutions`.
- Проверка `categoryId=41&lang=ru`: HTML содержит реальные поля `name="attribute[208]"`, value `211`, `3587` и др.
- Проверка `attributeId=208&value=3587`: отдал child controls с `attribute[211]`.
- Назначение: точные input names/галочки/радио для формы Kwork.

`GET https://kwork.ru/api/freeprice/categorygetprices`

- Auth: public/web.
- Параметры: `categoryId`, `lang`.
- Проверка `categoryId=41`: `prices.priceGradation`, `minPrice=500`, `maxPrice=71183`, `typicalPriceGradation`.

`GET https://kwork.ru/api/freeprice/attributegetprices`

- Auth: public/web.
- Параметры: `categoryId`, `attributeId`, `lang`.
- Проверка `categoryId=41&attributeId=208`: `success=true`, `prices=null`.

### Authenticated web state

`GET https://kwork.ru/projects`

- Auth: web cookies from Session Hub.
- Ответ: HTML with `window.stateData`.
- `stateData` had 218 top keys.
- Useful keys: `wantsListData`, `exchangeStats`, `categories`, `categoriesWithFavoritesList`, `connect`, `connectsPoints`, `filter`, `filters`, `counts`, `attributes`, `attributesCount`, `wantViews`.
- `exchangeStats`: `projects=8623`, `orders=4063`, `value=89836000`, `period=30`.
- `connect`: `connect_points=30`, `accrued_connect_points=30`, `connects_last_refill_date=2026-06-09 00:12:01`, `isPenalised=false`.
- `wantsListData.wants`: 12 active wants in page state.
- `wantsListData.attributesCount`: for current page/category mix, e.g. `3587=11`, `7352=18`, `5548694=8`.
- `wantsListData.favouriteCategories`: includes category `41` with attributes `211`, `3587`, `7352`, `3934090`, `4158112`, `4158119`, `5503256`, `5548694`.
- Назначение: fallback/source of truth for UI-like demand state and filters.

## JS-discovered web endpoints

Confirmed by JS bundles, not all invoked because some are state-changing:

- `POST /api/order/getorderscount`: with Session Hub cookies returned `{asPayer, hasPayerImportant, asWorker, payerWants}`; without web session returned `false`.
- `GET /api/user/checknotify`: returned `success=true`, `data.new_message`, `notify_unread_count`, `dialog_data`, `notifyTypeInfo`.
- `POST /api/user/checkworkeroffers`: returned `success=true`, `data.has_offers=false`.
- `GET /api/user/getworkbaysellers`: returned `success=true`, `data=null`.
- `GET /api/kwork/getkworksites`: params `kworkId`, `showHosts`, `offset`; returned `success`, `linksSites`, `orderedLinksSites`, `html`.
- `POST /api/offer/addview`: marks viewed wants, found in projects JS.
- `GET /wants/{wantId}/check_offer_notify`: with Session Hub cookies returned `success=true`, `data.categoryId`, `parentCategoryId`, `classificationId`, `isUserNeedPortfolio`, `isUserNeedKworkNotify`, `urlForCreatePortfolio`, `isNeural`.
- `POST /projects/check_is_template`: with Session Hub cookies and offer text returned `success=true`, `data=[]`; useful preflight before sending an offer.
- `POST /api/offer/addview`: with Session Hub cookies and `wantIds[]=3213259` returned `{"result": true}`; marks a want as viewed.
- `POST /wants/hide_want`: state-changing.
- `POST /projects/manage/restart`: state-changing.
- `GET /wants/{wantId}/check_offer_notify`: found in JS, candidate for offer notification check.

## Live Market Snapshot - 2026-07-08 15:41-15:44 MSK

Source: PSR `KworkMarketClient` against `api.kwork.ru`, Session Hub web cookies for `/projects`, Tavily map/crawl, and CDN JS endpoint extraction. No secret values were stored.

### Category tree

- `POST /categories` returned 7 root categories and 61 total category nodes.
- Root categories observed: Design (`15`), Development and IT (`11`), Texts and translations (`5`), SEO and traffic (`17`), Social media and marketing (`45`), Audio/video (`7`), Business and life (`83`).

### Supply samples from `/kworks`

These are point-in-time counts from live API and may drift.

| categoryId | kworks_count | important classifiers / slices |
|---:|---:|---|
| `41` | `38151` | Parsers `211` = `2900`, Chat bots `3587` = `16769`, Scripts `7352` = `7555`, Telegram Mini Apps `3934090` = `1838`, AI agents `5548694` = `991`, AI bots `4158112` = `2921`, ML `4158119` = `358`, IoT `5503256` = `190` |
| `72` | `3101` | Site visitors `199` = `2629`, Behavioral factors `200` = `577` |
| `71` | `1231` | Semantic core from scratch `3799` = `753`, by site `3800` = `362`, ready core `186454` = `124` |
| `59` | `45600` | Article/crowd links `1379472` = `19115`, forum links `1379027` = `6854`, profiles `1378848` = `10212` |
| `56` | `2148` | Metrics/counters `942` = `676`, site/market analysis `943` = `1477` |
| `44` | `2387` | SEO audit `1120` = `1479`, consultation `1121` = `779` |
| `43` | `1938` | Full optimization `478713` = `795`, page optimization `478714` = `558`, robots/sitemap `478716` = `197` |
| `273` | `2993` | No child classifiers returned; top cards are SEO/Yandex promotion offers |

Timing evidence: `/kworks` calls for these categories completed in about `2324-3810 ms` each without competitor detail enrichment.

### Current demand via authenticated web state

`GET https://kwork.ru/projects` with Session Hub cookies returned HTTP 200, HTML length about `309847`, and `window.stateData` with `218` top-level keys.

Important fields:

- `exchangeStats`: `projects=8623`, `orders=4063`, `value=89836000`, `period=30`.
- `wantsListData.wants`: 12 current want cards in page state.
- Current sample titles included: `Avtoobrabotka audio sales: Whisper + Claude dashboard`, `Nastroika AI zadach v konstruktore`, `AI agent dlya analiza finansovoi deyatelnosti`, `Nuzhno donastroit tg bota`, `AI content factory`, `Avtomatizatsiya kommentariev`.
- Current page demand categories included `41` and `112`.
- `wantsListData.attributesCount` top values: `7352=16`, `3587=11`, `5548694=8`, `1466231=8`, `4158112=8`, `211=6`, `1357=5`, `233=2`, `6413=1`.
- `wantsListData.filters` exposed `by_budget` and `by_kworks`.

Token-path note: mobile token endpoints `POST /projects` and `POST /getWantsCount` fail with parameter errors when a standalone script does not load `.env` and the API client falls back to Session Hub `cookie-only`. After explicit `.env` load, PSR `KworkService` initialized Session Hub as `email+cookies`; `getWantsCount(categories=all)` returned `530`, and `get_raw_projects(categories=all,page=1)` returned 12 projects with `connects` and `paging`. Practical PSR rule: use mobile `/projects` and `/getWantsCount` as the primary demand source when `.env` credentials are loaded; keep `/projects` `window.stateData` as web fallback and UI-state cross-check.

### Competitor / seller profile samples

`POST /userByUsername` is very valuable for market intelligence because it returns seller profile stats, kworks, skills, reviews and portfolio list without scraping HTML.

Live seller profile samples from top cards:

| username | id | rating | reviews_count | kworks_count | completed_orders_count | done % | repeat % | portfolio_count | skills_count |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `MDevostator` | `1354` | `5.0` | `1085` | `89` | `2864` | `100` | `76` | `0` | `11` |
| `Nekrashevich_Alex` | `3280` | `5.0` | `1761` | `89` | `2133` | `96` | `52` | `12` | `12` |
| `ProxyCorp` | `3194382` | `5.0` | `538` | `4` | `972` | `100` | `59` | `0` | `7` |
| `leaflex` | `598386` | `4.9` | `814` | `20` | `2752` | `99` | `68` | `0` | `0` |

### JS endpoint extraction batch

Fetched 4 pages (`/`, `/projects`, `/categories/programming`, `/categories/seo`), found 36 unique Kwork/CDN JS bundles, and extracted endpoint literals from 6 bundles.

New/important endpoints from this batch:

- `/projects/list/{username}`: buyer-specific projects list page. JS builds it as `"/projects/list/" + wantData.user.username`.
- `/portfolio/load_form_orders`, `/portfolio/load_form_orders_full`, `/portfolio/load_form_kworks`: seller portfolio/order/kwork binding data; useful for seller proof and card analysis.
- `/portfolio/get_popup`, `/portfolio/delete`, `/portfolio/can_delete/`: portfolio popup/control endpoints; useful but some are state-changing.
- `/api/file/upload`, `/api/kwork/deleteattachment`: offer/portfolio attachment flow; state-changing.
- `/api/user/setworkerstatusswitchall`, `/api/user/switchworkerstatus`: seller status controls; state-changing.
- `/api/cart/add`, `/api/cart/order`, `/api/cart/delete`: buyer cart flow; not primary for seller market intelligence.

## PSR Capability Gap

PSR currently has a strong point query: selected category/classifier -> supply count, classifiers, top competitor cards, optional demand, optional competitor details. It does not yet have a full-market intelligence engine.

Recommended next implementation:

1. Add a `KworkMarketIntelligenceScanner` job that walks the category tree, calls `/kworks` for each category and top classifier, and stores daily supply snapshots.
2. Add demand fallback: first try mobile `/getWantsCount` and `/projects`; if cookie-only or parameter errors occur, parse `/projects` `window.stateData`.
3. Add seller enrichment queue: for top cards, call `userByUsername`, `userKworks`, `getKworkDetailsExtra`, `getKworkReviews`, and `getKworkAnswers`.
4. Add niche scoring: rank by demand attributes from `wantsListData.attributesCount`, supply from `/kworks`, seller saturation, top seller repeat/review density, and median visible price.
5. Persist snapshots so PSR can answer "what is growing now" instead of only "what is visible now".

### `/projects/list/{username}` validation

JS context: `wantsUserListUrl` returns `"/projects/list/".concat(toLower(wantData.user.username))`.

Live check with Session Hub cookies:

- `GET /projects/list/Evdohaanna2403`: HTTP 200, HTML, `stateData` 194 keys, `wantsListData` exists, `wants_len=0`.
- `GET /projects/list/smm_vk`: HTTP 200, HTML, `stateData` 194 keys, `wantsListData` exists, `wants_len=0`.
- `GET /projects/list/grandlers`: HTTP 200, HTML, `stateData` 194 keys, `wantsListData` exists, `wants_len=0`.

Conclusion: this is not the primary whole-market demand feed. It is still useful for buyer-specific history/context when `wantsListData` contains entries, but `/projects` page state is the better current source for "what is popular now".

## Additional Mobile Catalog APIs - 2026-07-08 15:55-16:00 MSK

These endpoints come from the installed `kwork` package `OpenAPIMethodsMixin` and were validated live where noted.

`POST https://api.kwork.ru/catalogMainv2`

- Auth: mobile basic.
- Params: none observed.
- Live response: `success=true`, `response` dict with `popular_categories_block`, `catalog_service_block`, `other_services_block`.
- `popular_categories_block`: 3 sections and 74 category/classifier items.
- Each item includes `category_id`, optional `classifier_id`, `name`, `cover_url`, `kworks_count`, `order`.
- Top live supply items by `kworks_count`: Marketplace design `category_id=286 classifier_id=1433413 count=82696`; Links `category_id=59 count=45547`; Ready databases `category_id=113 classifier_id=1117 count=34073`; Logos `category_id=25 classifier_id=401928 count=33636`; Marketplaces `category_id=112 classifier_id=1357 count=21114`; Illustrations `category_id=28 classifier_id=819 count=18150`; Presentations `category_id=270 classifier_id=314398 count=11605`; Web design `category_id=24 classifier_id=393348 count=11331`; Telegram `category_id=46 classifier_id=281 count=7157`; AI logos/infographics `category_id=306 classifier_id=4200156 count=6642`.
- Market value: very high. It is a fast curated seed list for a full-market scanner.

`POST https://api.kwork.ru/catalogRubrics`

- Auth: mobile basic.
- Params: none observed.
- Live response: 7 rubrics with `id`, `name`, `order`, `rubric_description`, images.
- Market value: medium; use as top-level menu/rubric metadata.

`POST https://api.kwork.ru/catalogCategories`

- Auth: mobile basic.
- Required param: `rubricId` in camelCase.
- Failed params: `rubric_id`, `categoryId`, `category_id`, `parentId`, `parent_id`, `id`.
- Live response for `rubricId=11`: `success=true`, 9 categories with `id`, `name`, `kworks_count`, `order`, `rubric_description`.
- Market value: high; can walk rubrics without parsing the full category tree.

`POST https://api.kwork.ru/kworksCategoriesList`

- Auth: mobile basic.
- Param validated: `user_id`.
- Live response for `user_id=3280`: `success=true`, 13 seller category tabs with `id`, `name`, `kworks_count`, `kworks`.
- Market value: high for competitor seller inventory by category.

`POST https://api.kwork.ru/userKworks`

- Auth: mobile basic worked in live check.
- Params validated: `user_id`, `page`.
- Live response for `user_id=3280&page=1`: `success=true`, 10 kwork cards plus `paging`.
- Card fields include `id`, `title`, `price`, `category_id`, `category_name`, `cover`, `image_url`, `share_url`, `status_id`, `status_name`, `worker`, `badges`, `is_best`, `is_favorite`, `is_hidden`.
- Market value: high for competitor card inventory.

`POST https://api.kwork.ru/portfolioList`

- Auth: mobile basic according to library, but parameter discovery is incomplete.
- Tried `user_id`, `userId`, `username`, `worker_id`, `seller_id`, `id`, `type`, `portfolio_type`, `category_id`, `categoryId`; responses were `Недостаточно параметров` or `Некорректные значения параметров`.
- Practical fallback: `userByUsername` already returns `portfolio_list`; web `/portfolio/*` loaders can supplement own-account portfolio binding context.

`POST https://api.kwork.ru/userReviews`

- Auth: token+basic according to library. Parameter discovery incomplete.
- Tried `user_id`, `userId`, `id`, `username`, `type`, `review_type`, `filter`; responses were `Недостаточно параметров` or `Некорректные значения параметров`.
- Practical fallback: `userByUsername` returns recent `reviews` for seller/buyer profile analysis.

## Portfolio Web Loader Validation - 2026-07-08

JS context from `projects-list-dcl` showed exact methods and params:

- `POST /portfolio/load_form_orders` with data `{kworkId}`.
- `POST /portfolio/load_form_orders_full` with data `{kworkIds}`.
- `POST /portfolio/load_form_kworks` with data `{categoryId, attributesId}`.
- `POST /portfolio/get_popup` with data `{portfolioId}`.

Live Session Hub checks:

- `POST /portfolio/load_form_kworks` with `categoryId=41`, `attributesId=3587` and with `attributesId[]=3587`: HTTP 200 JSON, `success=true`, `data.kworks=[]`, `data.hasOrders=false`.
- `POST /portfolio/load_form_orders` with `kworkId=29072946`: HTTP 200 JSON, `success=true`, `data.anotherVideos=[]`, `data.hasOrders=false`.
- `POST /portfolio/load_form_orders_full` with `kworkIds=29072946`: HTTP 200 JSON, `success=true`, `data.orders=[]`.
- `POST /portfolio/get_popup` with portfolio id `14040541` from `userByUsername(Nekrashevich_Alex)`: HTTP 404 JSON `"Страница не найдена"`. Likely needs a web-specific portfolio id/context or only works for accessible own/modal records.

Market value: loaders are not a whole-market source, but they help understand seller proof/portfolio binding and can support future portfolio-aware draft generation.
- `POST /wants/review/save`: state-changing review.
- `POST /api/file/upload`: upload endpoint.
- `POST /api/kwork/deleteattachment`: state-changing.
- `GET /api/portfolio/youtubelinkvalidate?url=...`: validation endpoint.
- `/portfolio/load_form_orders`, `/portfolio/load_form_orders_full`, `/portfolio/load_form_kworks`: portfolio form loaders.

## Practical integration decisions

1. Fast Kwork Market screen should call `POST /kworks` with `categoryId`, `classifierId`, `page`; do not load competitor details automatically.
2. Use `classifierId`, not `attribute[...]`, as the primary narrowing mechanism for competitor relevance.
3. Use `getKworkDetails` and optionally `getKworkDetailsExtra`, `getKworkAnswers`, `getKworkReviews` only on demand or capped background enrichment.
4. Use `projects` and `getWantsCount` for demand. They require token auth.
5. Use `GET /projects` stateData as a web-auth fallback and as a rich source for exchange stats, filters, favourite categories and counts.
6. Use `loadclassification` for exact form controls and `freeprice/*` for price steps.
7. Keep Session Hub running for realistic authenticated web behavior.

## Next candidates to inspect

- `POST /applyFilters`, `POST /clearFilters`: seller exchange filters. They may change account filter state; inspect with caution.
- `GET /wants/{id}/check_offer_notify`: confirmed useful before offer generation.
- `POST /api/offer/createoffer`, `POST /wants/create_offer_draft`, `POST /projects/check_is_template`: offer flow; useful but stateful.
- `POST /userKworks`, `POST /userReviews`, `POST /portfolioList`: seller/competitor analytics.
- `POST /getHiddenKworks`, `POST /favoriteKworks`: personalization/history.
- `POST /workerOrders`, `POST /payerOrders`, `POST /notifications`: account workflow analytics.

## Live API Probe Addendum - 2026-07-08 16:20 MSK

Generated snapshot: `docs/kwork_api_probe_snapshot_2026-07-08.json` (no secrets, cookies, tokens, Authorization headers, or password values).

New validated market-useful endpoints:

- `POST https://api.kwork.ru/catalogMain` with token+basic auth. Params: none observed. Response keys: `rubrics`, `popular_categories`, `popular_kworks`, `viewed_kworks`. Live counts: 7 rubrics, 4 popular category groups, 20 popular kworks, 3 viewed kworks. This is useful for current account-personalized catalog context and popular kwork-card sampling.
- `POST https://api.kwork.ru/catalogFilters` with basic auth. Param: `categoryId`. Live responses for categories `41`, `59`, `56`, `44`, `72`, `113`, `286`, `306` returned `response.filters`, `response.groups`, and `response.kworks_count`. This is a second, JSON-only source of catalog filter metadata; compare with `/categoryAttributes` and `/api/attribute/loadclassification`.
- `POST https://api.kwork.ru/category` with basic auth. Param must be `category_id` (snake case). `id`, `categoryId`, `catId`, `rubricId` returned "not enough parameters". Response keys include `id`, `name`, `mobile_description`, `base_volume`, `volume_type`, `attributes`, `positive_reviews_count`.
- `POST https://api.kwork.ru/positiveReviewsCount` with basic auth. Param must be `category_id`. `categoryId`, `id`, and `categoryId+classifierId` returned `response=false`. With `category_id`, response is a dict keyed by attribute/review buckets such as `1`, `5`, `10`, `20`, `50`, `100`.
- `POST https://api.kwork.ru/searchKworksCatalogQuery` with basic auth. Param: `query`. Live terms `telegram`, `seo`, `python` returned 9 suggestions each; short/ru terms tested in the Windows console produced 0 in this run, likely due console encoding for the inline script. Use UTF-8 request construction from Python/PSR for Russian terms.
- `POST https://api.kwork.ru/user` with basic auth. Param: `id`. Sample `id=3280` returned public user fields including `id`, `username`, `status`, `fullname`, `profilepicture`, `description`, `slogan`, `location`, `rating`, `rating_count`, `level_description`, `good_reviews`.
- `POST https://api.kwork.ru/userSearch` with basic auth. Param: `query`. Sample `query=Nekrashevich` returned 31 users plus `paging`; useful for competitor lookup by partial username/name.
- `POST https://api.kwork.ru/viewedCatalogKworks` with token+basic auth. Params: none/page optional. Live account sample returned 3 viewed catalog kworks plus `paging`; useful for personal workflow/history, not whole-market scoring.
- `POST https://api.kwork.ru/getKworkPortfolios` with basic auth. Param: `id` (kwork id), optional `page`. Sample `id=29072946` returned `success=true`, empty `response[]`, and `paging`. `kwork_id` and `kworkId` did not work.

`catalogMainv2` parser correction:

- `popular_categories_block` is nested: it is a list of themed sections, each section has `categories[]`.
- Recursive extraction found 84 category/classifier items across `catalogMainv2`, including service/rubric and "other services" entries.
- Top supply seeds from the corrected snapshot: marketplace design `category_id=286 classifier_id=1433413 kworks_count=82696`; links `category_id=59 kworks_count=45547`; ready databases `category_id=113 classifier_id=1117 kworks_count=34073`; logos `category_id=25 classifier_id=401928 kworks_count=33636`; marketplaces `category_id=112 classifier_id=1357 kworks_count=21114`; illustrations `category_id=28 classifier_id=819 kworks_count=18150`; presentations `category_id=270 classifier_id=314398 kworks_count=11605`; photo editing `category_id=68 classifier_id=399860 kworks_count=11495`; web design `category_id=24 classifier_id=393348 kworks_count=11331`; social media design `category_id=286 classifier_id=1433356 kworks_count=8304`.

Rubric walk:

- `catalogRubrics` returned 7 live rubrics.
- Calling `catalogCategories(rubricId=<live id>)` for all 7 rubrics returned 54 category records total.
- This is smaller than the `POST /categories` tree count (61 nodes), so the scanner should use both: `/categories` for full tree coverage and `catalogRubrics/catalogCategories` for mobile catalog/menu metadata.

Still unresolved after focused parameter probing:

- `POST /portfolioList`: simple params `user_id`, `userId`, `id`, `username`, `login`, `worker_id`, with/without `page`, still returned "not enough parameters". Continue using `userByUsername.portfolio_list` and web portfolio loaders.
- `POST /userReviews`: simple params `user_id`, `userId`, `id`, `username`, `login`, `worker_id`, with/without `page`, still returned "not enough parameters". Continue using `userByUsername.reviews` and kwork-level `getKworkReviews`.
- `POST /getUserInfo`: simple user params returned invalid/unknown API error in this run. `POST /user` and `POST /userByUsername` are the useful public profile endpoints.

Tavily/public crawl result:

- Tavily map with category/project/user/kwork/portfolio/api path filters still returned only the 7 main public category URLs. No new public API-like paths were found through crawl. The valuable hidden layer is currently mobile API wrappers, authenticated `window.stateData`, and JS bundle literals.

Updated scanner recommendation:

1. Seed full-market supply from `catalogMainv2` top items plus `/categories` tree plus `catalogRubrics/catalogCategories`.
2. For each seed, call `/kworks` by `categoryId` and the deepest `classifierId` where present.
3. Pull filters from both `/categoryAttributes` and `/catalogFilters`, then use `/api/attribute/loadclassification` only when exact form field names are needed.
4. Pull demand from token `/projects` and `/getWantsCount`, with web `/projects` `window.stateData` as fallback/cross-check.
5. Enrich competitors with `/user`, `/userByUsername`, `/userSearch`, `/userKworks`, `/kworksCategoriesList`, `/getKworkDetails`, `/getKworkDetailsExtra`, `/getKworkReviews`, `/getKworkAnswers`, and `/getKworkPortfolios`.

## Demand And Competitor Snapshot Addendum - 2026-07-08 16:50 MSK

New local artifacts:

- `docs/kwork_api_docs_probe_2026-07-08.json`
- `docs/kwork_api_docs_probe_auth_2026-07-08.json`
- `docs/kwork_api_docs_probe_post_auth_2026-07-08.json`
- `docs/kwork_demand_probe_snapshot_2026-07-08.json`
- `docs/kwork_supply_competitor_snapshot_2026-07-08.json`

API documentation discovery:

- Standard REST doc paths were checked: `/swagger.json`, `/api-docs`, `/openapi.json`, `/openapi.yaml`, `/v1/api-docs`, `/v2/api-docs`, `/.well-known/openapi.json`, `/docs`, `/redoc`, `/swagger-ui`.
- Without mobile Authorization, `api.kwork.ru` returns nginx 401.
- With the mobile Authorization header used by the `kwork` package, GET returns JSON error "only POST allowed"; POST returns JSON "API method not found".
- Standard SOAP/WSDL paths (`/service?wsdl`, `/Service.svc?wsdl`, `/ws/service.wsdl`, `/axis2/services/listServices`, `/cxf/services`) behaved the same. No Swagger/OpenAPI/WSDL document was found.

Demand snapshot:

- `POST /getWantsCount` and `POST /projects` were run by category and query with token+basic auth.
- Current live counts from `/getWantsCount`: all market `570`; programming `41` -> `49`; links `59` -> `2`; marketplace/social design `286` -> `15`; databases/content `113` -> `5`; marketplaces `112` -> `19`; social/bots `46` -> `51`; AI `306` -> `1`; logos/branding `25` -> `10`; SEO audit `44` -> `2`; site/market analysis `56` -> `5`; traffic `72` -> `8`.
- Query counts: `telegram=39`, `ai=11`, `seo=23`, `python=5`, `marketplace=0`, `bot=1`, `logo=9`, `database=0`.
- Across sampled project pages, 126 unique projects were seen. Top sampled categories: `41` (26), `46` (20), `25` (16), `286` (13), `112` (13), `72` (8), `38` (7), `37` (6).

Demand detail endpoints:

- `POST /project` with `id=<want_id>` works and returns a dict with keys such as `id`, `status`, `user_id`, `username`, `profile_picture`, `price`, `title`, `description`, `offers`, `time_left`, `parent_category_id`, `category_id`, `date_confirm`, `user_projects_count`, `user_hired_percent`, `user_active_projects_count`, `achievements_list`, `is_viewed`, `already_work`, `allow_higher_price`, `possible_price_limit`, `has_offer`, `user_need_kwork`, `user_need_portfolio`, `user_need_portfolio_rubric_name`.
- `POST /want` with `id=<want_id>` works and returns a list length 1 for sampled ids. It includes complementary want metrics such as `views`, `date_create`, `views_history`, and order/history-style fields.
- Practical rule: use `/projects` for list pages, `/project` for current buyer/project card and offer decision fields, `/want` for historical/view counters.

Supply/competitor snapshot:

- `POST /kworks` was run for 8 top seeds from `catalogMainv2`: marketplace design, links, ready databases, logos, marketplaces, Telegram, AI logos/infographics, broad programming.
- The snapshot collected 80 kwork cards and 54 unique sellers from seed first pages.
- Top repeated sellers in seed pages: `Vitaliy_DSGN` (3 cards), `Mikhail_Aksenov`, `IG_designed`, `IrinaDana`, `MDevostator`, `Maxximus`, `Kristi86`, `sbor_kontaktov`, `imnotes`, `LittleSani`, `DESIGN-LOGO`, `DESIGN_LOGOTYPE`, `TopClick`, `ameganix` (2 cards each).
- Seed page visible price range in the sample: `500` to `50000`.
- Enrichment used `userByUsername`, `kworksCategoriesList`, `userKworks`, `getKworkDetails`, `getKworkDetailsExtra`, `getKworkReviews`, `getKworkAnswers`, `getKworkPortfolios`.
- This validates the scanner path: seed -> kworks cards -> seller profile/tabs/inventory -> kwork detail/reviews/FAQ/portfolio.

Current PSR gap after this pass:

- PSR can fetch point metrics for one selected category/classifier. It still needs a persisted daily scanner to turn these snapshots into trend data.
- Minimum useful scanner schema: `snapshot_time`, `source_endpoint`, `seed_name`, `category_id`, `classifier_id`, `kworks_count`, `want_count`, `query`, `top_cards`, `top_sellers`, `project_samples`, `detail_enrichment_status`.
- Daily ranking should combine `/getWantsCount` demand, `/kworks` supply, `catalogMainv2` supply seed weight, seller repeat/review density from `userByUsername`, and project list recency from `/projects`.

## PSR Scanner Implementation - 2026-07-08 16:58 MSK

Implemented a repeatable backend scanner around the API map discovered above.

New backend capability:

- `KworkMarketClient.get_market_intelligence_snapshot(...)` in `src/platforms/kwork_market.py`.
- `POST /api/kwork/market/intelligence-snapshot` in `src/api/routes/kwork.py`.
- Default output directory when `write_file=true`: `docs/kwork_market_snapshots/`.

Scanner behavior:

- Discovers market seeds from `POST /catalogMainv2` recursively; if discovery fails, falls back to the known high-value seed list.
- Calls `POST /kworks` for each seed and page.
- Aggregates card count, unique sellers, top sellers, category counts and per-seed first-page classifiers/cards.
- Optionally calls demand endpoints (`/getWantsCount`, `/projects`) by seed category and query terms.
- Optionally enriches competitor cards with `getKworkDetails` through the existing detail path.
- Writes a timestamped JSON snapshot with no cookies, tokens, Authorization headers or password values.

Live smoke:

- Command path: direct `KworkMarketClient.get_market_intelligence_snapshot(max_seeds=2, pages=1, include_demand=False, write_file=True)`.
- Output file: `docs/kwork_market_snapshots/kwork_market_intelligence_20260708T135633Z.json`.
- Result: `seed_count=2`, `cards_seen=20`, `unique_sellers_seen=18`.
- First seed came from live `catalogMainv2`: `Дизайн для маркетплейсов`, `category_id=286`, `classifier_id=1433413`, seed supply count `82701`; `/kworks` returned `82945`.

Verification:

- `PYTHONPATH=C:\psr pytest tests/unit/test_kwork_market.py tests/unit/test_kwork_routes.py -q`
- Result: `18 passed`.

## Desktop Scanner UI - 2026-07-08 17:07 MSK

Implemented the desktop entry point for the market scanner.

New desktop capability:

- `desktop/src/lib/api.ts`: `api.getKworkMarketIntelligenceSnapshot(...)` calls `POST /api/kwork/market/intelligence-snapshot`.
- `desktop/src/lib/api.ts`: typed `KworkMarketIntelligenceRequest` and `KworkMarketIntelligenceSnapshot`.
- `desktop/src/pages/KworkMarket.tsx`: top toolbar now has a `Market scan` button.
- `Market scan` runs a whole-market snapshot with `max_seeds=8`, `pages=1`, `include_demand=true`, `include_competitor_details=false`, `write_file=true`.
- The page shows generated time, seed count, sampled card count, unique seller count, runtime, top sellers and the written JSON path.

Verification:

- `npx vite build` in `desktop`: passed.
- `npx tsc --noEmit` in `desktop`: failed on pre-existing unrelated pages (`Conversations`, `Conversion`, `Dashboard`, `Health`, `Orders`, `Skipped`); no `KworkMarket.tsx` or new Kwork API helper errors were reported.
- `PYTHONPATH=C:\psr pytest tests/unit/test_kwork_market.py tests/unit/test_kwork_routes.py -q`: `18 passed`.

## Seller Intelligence Scanner - 2026-07-08 17:21 MSK

Extended the scanner from card-level supply to seller-level intelligence.

New backend capability:

- `KworkMarketClient.fetch_seller_intelligence(username)` calls `userByUsername`, then `kworksCategoriesList(user_id)` and `userKworks(user_id,page=1)` when a user id is available.
- `KworkMarketClient.enrich_sellers_from_competitors(...)` selects top repeated sellers from sampled cards and enriches them with profile, category tabs and inventory samples.
- `get_market_intelligence_snapshot(...)` now accepts `include_seller_details` and `seller_detail_limit`.
- Snapshot output now includes top-level `seller_intelligence` and `aggregate.seller_profiles_collected`.

Why this matters:

- `userByUsername` gives seller public profile facts such as id, username, level, rating/review counters, description-adjacent profile fields, skills and portfolio counts.
- `kworksCategoriesList` gives the seller's category spread.
- `userKworks` gives seller inventory cards without opening public HTML pages.
- Combined with `/kworks` seed pages, this identifies repeat sellers and what else they sell across the market.

Live smoke:

- Seed: `Telegram`, `category_id=46`, `classifier_id=281`.
- Output file: `docs/kwork_market_snapshots/kwork_market_intelligence_20260708T142059Z.json`.
- Result: `seed_count=1`, `cards_seen=10`, `unique_sellers_seen=9`, `seller_profiles_collected=2`.
- Seller samples: `imnotes` had 5 category tabs and 6 sampled inventory kworks; `RuPartner` had 4 category tabs and 6 sampled inventory kworks.

Verification:

- `PYTHONPATH=C:\psr pytest tests/unit/test_kwork_market.py tests/unit/test_kwork_routes.py -q`: `19 passed`.
- `npx vite build` in `desktop`: passed.
- `npx tsc --noEmit` still fails on unrelated pre-existing desktop pages listed in the previous section; no new Kwork scanner errors were reported.

## Market Ranking Analytics - 2026-07-08 17:29 MSK

Extended each snapshot with deterministic niche ranking fields.

New snapshot fields:

- Top-level `market_rankings`.
- `aggregate.top_opportunities`.

Ranking row fields:

- `seed_name`, `category_id`, `classifier_id`.
- `supply_kworks_count` from `/kworks`.
- `demand_wants_count` from `/getWantsCount` through the demand snapshot path.
- `demand_per_1000_kworks`.
- `opportunity_score`, computed from demand count and supply count.
- `sample_price_min`, `sample_price_max`, `sample_price_avg`.
- `sample_unique_sellers`, `seller_concentration`, `top_sellers`.
- `signals.demand_level` and `signals.supply_level`.

Live smoke with `.env` loaded:

- Output file: `docs/kwork_market_snapshots/kwork_market_intelligence_20260708T142853Z.json`.
- Seed: `Telegram`, `category_id=46`, `classifier_id=281`.
- Supply: `8780` kworks.
- Demand: `51` wants for category `46`; query demand `telegram=39`.
- Ranking: `demand_per_1000_kworks=5.809`, `opportunity_score=391.98`, `demand_level=high`, `supply_level=moderate`.
- Sample prices: min `500`, max `25000`, avg `3700.0`.

Tavily check:

- `tavily_map` was run against `https://kwork.ru` with strict public path filters for categories, projects, users, portfolios, kwork pages and API-like paths.
- Result: no URLs returned under those strict filters. Practical conclusion: the useful hidden market endpoints are not discoverable through public crawler paths; continue prioritizing mobile API, web JSON routes, JS endpoint extraction and authenticated Session Hub/API calls.

Verification:

- `PYTHONPATH=C:\psr pytest tests/unit/test_kwork_market.py tests/unit/test_kwork_routes.py -q`: `20 passed`.
- `npx vite build` in `desktop`: passed.
- `npx tsc --noEmit` still fails on the same unrelated pre-existing desktop pages; no new Kwork scanner errors were reported.

## Snapshot History Index - 2026-07-08 17:37 MSK

Added persistent compact history artifacts for trend tracking.

New write behavior when `write_file=true`:

- Full timestamped snapshot remains unchanged: `docs/kwork_market_snapshots/kwork_market_intelligence_<UTC>.json`.
- `docs/kwork_market_snapshots/latest.json` is overwritten with a compact summary of the latest run.
- `docs/kwork_market_snapshots/index.jsonl` receives one compact summary line per run.

Compact summary contents:

- `generated_at`, `file_path`, `source`, compact `config`.
- Aggregate counts: seeds, cards, unique sellers, seller profiles.
- Top opportunities, top sellers, query demand counts and timings.

Live smoke:

- Full file: `docs/kwork_market_snapshots/kwork_market_intelligence_20260708T143644Z.json`.
- Latest file: `docs/kwork_market_snapshots/latest.json`.
- History file: `docs/kwork_market_snapshots/index.jsonl`.
- Smoke seed: `Telegram`, `category_id=46`, `classifier_id=281`.
- Result: `cards_seen=10`, `unique_sellers_seen=9`, latest/index both contain the compact summary.

Verification:

- `PYTHONPATH=C:\psr pytest tests/unit/test_kwork_market.py tests/unit/test_kwork_routes.py -q`: `20 passed`.
- `npx vite build` in `desktop`: passed.
- `npx tsc --noEmit` still fails on unrelated pre-existing desktop pages; no new Kwork scanner errors were reported.

## Snapshot History Reader - 2026-07-08 17:45 MSK

Added API/UI access to the compact market scan history.

New backend capability:

- `KworkMarketClient.get_market_intelligence_history(limit=50, output_dir=None)`.
- `GET /api/kwork/market/intelligence-history?limit=<n>`.
- Reads `docs/kwork_market_snapshots/latest.json` and the tail of `docs/kwork_market_snapshots/index.jsonl`.
- Skips malformed JSONL lines instead of failing the whole response.

New desktop capability:

- `api.getKworkMarketIntelligenceHistory(limit)`.
- Kwork Market toolbar has a `History` button.
- UI shows entry count, latest timestamp, last entries, cards/sellers counts, top opportunity and `index.jsonl` path.

Live local reader check:

- `exists=true`.
- `entry_count=1`.
- `latest_generated_at=2026-07-08T14:36:44Z`.
- Paths: `docs/kwork_market_snapshots/latest.json`, `docs/kwork_market_snapshots/index.jsonl`.

Verification:

- `PYTHONPATH=C:\psr pytest tests/unit/test_kwork_market.py tests/unit/test_kwork_routes.py -q`: `22 passed`.
- `npx vite build` in `desktop`: passed.
- `npx tsc --noEmit` still fails on unrelated pre-existing desktop pages; no new Kwork scanner errors were reported.

## Web JS Endpoint Extraction - 2026-07-08 17:49 MSK

Ran a public HTML/JS endpoint extractor against Kwork web pages and CDN bundles.

Input pages:

- `https://kwork.ru/`
- `https://kwork.ru/projects`
- `https://kwork.ru/categories/programming`
- `https://kwork.ru/categories/design`
- `https://kwork.ru/categories/marketing`

Artifacts:

- JS extraction snapshot: `docs/kwork_js_endpoint_snapshot_2026-07-08T144640Z.json`.
- Live status sample: `docs/kwork_js_endpoint_live_status_2026-07-08T144816Z.json`.

Extraction result:

- Sources inspected: `51`.
- JS scripts fetched: `46`.
- Endpoint-like paths extracted: `348`.
- Interesting paths after filtering: `333`.
- Filtered new web/API-like paths not already in the hidden API facts: `37`.

New useful web endpoint candidates from JS:

- `/api/order/getorderscount`
- `/api/user/getworkbaysellers`
- `/api/bill/address_suggestions`
- `/api/offer/addview`
- `/api/file/upload`
- `/api/cart/add`, `/api/cart/delete`, `/api/cart/order`
- `/api/portfolio/hidelimitnotice`
- `/api/portfolio/youtubelinkvalidate?url=`
- `/portfolio_large/`
- `/portfolio/log_portfolio_view`
- `/portfolio/add`
- `/portfolio/upload_image`, `/portfolio/upload_video`, `/portfolio/upload_audio`, `/portfolio/upload_pdf`
- `/portfolio/alternate_upload_cover`, `/portfolio/alternate_upload_image`, `/portfolio/alternate_upload_video`
- `/portfolio/check_audio_ready`, `/portfolio/check_video_thumbnail_ready`, `/portfolio/check_gif_thumbnail_ready`
- `/catalog_kworks_filters/`

Live status sample facts:

- `GET /api/user/getworkbaysellers`: HTTP 200 JSON, `success=false`, auth required.
- `GET /api/bill/address_suggestions`: HTTP 200 JSON, auth error payload.
- `GET /portfolio_large/`: HTTP 302 to `http://kwork.ru/portfolio_large`.
- `GET /catalog_kworks_filters/`: HTTP 302 to `http://kwork.ru/catalog_kworks_filters`.
- `GET /api/order/getorderscount`, `/api/portfolio/youtubelinkvalidate?...`, `/api/user/checklogin`: HTTP 200 HTML maintenance page without required method/context.

Tavily cross-check:

- `tavily_crawl` without research mode returned public category pages only (`audio-video`, `business`, `design`, `programming`, `promotion`, `seo`, `writing-translations`).
- No API-like URLs were discovered by Tavily crawl. JS bundle extraction is the better source for hidden web endpoints.

## Priority JS Endpoint Context - 2026-07-08 17:53 MSK

Extracted deeper code context around priority JS-discovered endpoint candidates.

Artifacts:

- Context extraction: `docs/kwork_js_priority_endpoint_context_2026-07-08T145218Z.json`.
- Safe method probe: `docs/kwork_js_priority_endpoint_probe_2026-07-08T145245Z.json`.

Confirmed context hints:

- `/api/order/getorderscount`: JS context includes `getActiveOrdersCount`, `ajaxGetActiveOrdersCount`, `updateOrdersCount`; GET/POST hints.
- `/api/user/getworkbaysellers`: JS context includes `getWorkbaySellers`, `sellers`, payment/company-modal context; GET/POST hints.
- `/api/offer/addview`: JS context includes `wantAddViews`, `wantIds`; POST hint.
- `/api/file/upload`: JS context includes `uploadFile`, `updateFileIndex`, `cancelToken`, `onUploadProgress`; POST hint.
- `/api/cart/add`, `/api/cart/delete`, `/api/cart/order`: JS context includes cart add/delete/order flow and order/refill fields; mutating endpoints, not probed beyond static extraction.
- `/api/portfolio/youtubelinkvalidate?url=`: JS context includes params `url`, `kwork_id`, `portfolio_id`; POST hint.
- `/portfolio_large/`: JS context includes `getPortfolio`, `getFirstPortfolio`, `setAllIds`, `standAlone`; likely portfolio popup/detail loader.
- `/portfolio/log_portfolio_view`: JS context includes `portfolio_video_id`, `view_type`; POST analytics endpoint.
- Portfolio upload/check endpoints include upload entity type and media readiness checks.
- `/catalog_kworks_filters/`: JS context includes `updateCatalogParams`, `aliasWithFilterPostParams`, `excludeIds`, `onePage`, `showMoreCount`, `getAliasWithFilterParamsPostFormData`.

Safe probe results:

- `POST /api/order/getorderscount`: HTTP 200 JSON, body `false`. Confirms method-sensitive JSON endpoint.
- `POST /api/portfolio/youtubelinkvalidate?url=...`: HTTP 401 JSON, access denied. Confirms method-sensitive JSON endpoint.
- `GET /api/user/getworkbaysellers`: HTTP 200 JSON auth-required payload; `POST` without proper context returned maintenance HTML.
- `POST /portfolio_large/` and `POST /catalog_kworks_filters/` without canonical URL/context returned HTTP 302 canonical redirects.

## Read-Like Web Endpoint Validation - 2026-07-08 18:00 MSK

Validated the canonical URL forms for two JS-discovered read-like endpoints.

Artifacts:

- `docs/kwork_readlike_endpoint_probe_2026-07-08T145632Z.json`
- `docs/kwork_readlike_endpoint_summary_2026-07-08T150029Z.json`

Confirmed facts:

- `POST /catalog_kworks_filters/programming` with `page=1&pageSize=10` returned HTTP 200 JSON, `success=true`, about 319 KB.
- `POST /catalog_kworks_filters/design` with `page=1&pageSize=10` returned HTTP 200 JSON, `success=true`, about 407 KB.
- The successful `catalog_kworks_filters` responses contain `data.stateData.viewData`.
- `stateData` keys observed: `viewData`, `isWidePage`, `isRedesign`, `isRedesignCard`, `isRedesignFilter`, `isShowSticker`, `isNeedYescrowDigitalSign`.
- `viewData` keys observed: `filters`, `kworks`.
- Useful filter keys observed include `excludeIds`, `sdeliverytime`, `sMinReview`, `sOrdersQueue`, `sMinUserSales`, `sMinKworkSales`, `sellerLvl`, `swithreviews`, `sonline`, `price`, `filterPrice`, `kworksCount`, `attributesIds`, `attributesTree`, `selectedAttributes`, `filterAttributes`, `selectedAttributesIds`, `multipleAttributes`, `paymentTypeFilter`, `attributeReviews`, `packageFilters`, `activeCategoryId`, `priceLimits`, `priceFilterBounds`, `queryParamsJson`, `volumeTypes`, `activeCat`.
- `POST /catalog_kworks_filters/telegram-boty` returned HTTP 200 JSON, `success=false`, `data=null`; alias must match Kwork's canonical catalog alias, not an arbitrary transliteration.
- `GET /portfolio_large/1123924` returned HTTP 200 HTML, about 4 KB, with portfolio/kwork markers and `control-overlay` navigation.
- `POST /portfolio_large/1123924` also returned HTTP 200 HTML in the earlier probe.
- `GET /portfolio_large/14040541` returned HTTP 200 HTML, about 4 KB, with portfolio/kwork markers and `control-overlay` navigation.
- `POST /portfolio_large/29072946` returned HTTP 404 JSON `"Page not found"` in the earlier probe; the endpoint expects a portfolio id, not a kwork id.

Practical value:

- `/catalog_kworks_filters/{alias}` is a strong same-origin web fallback for catalog UI state: kwork cards plus the web filter model in one JSON response.
- `/portfolio_large/{portfolio_id}` is a direct portfolio detail/popup loader. It can enrich competitor portfolio samples when portfolio ids are already available from mobile `userByUsername.portfolio_list` or web cards.

## Catalog Alias Sweep - 2026-07-08 18:06 MSK

Purpose: discover canonical `/categories/{alias}` values that can be reused with `POST /catalog_kworks_filters/{alias}`.

Artifacts:

- `docs/kwork_catalog_alias_probe_2026-07-08T150507Z.json`
- `docs/kwork_catalog_alias_deep_probe_2026-07-08T150630Z.json`

Method:

- Tavily `map`/`crawl`, without research mode, again found only the 7 public root category URLs.
- Direct HTML extraction from the 7 root category pages found 61 `/categories/{alias}` candidates.
- A low-data POST check was run against `/catalog_kworks_filters/{alias}` with `page=1&pageSize=10`.

Results:

- 61 aliases were extracted.
- 18 aliases returned HTTP 200 JSON `success=true`.
- 43 later probes returned HTTP 403 HTML of 1726 bytes. A follow-up single-session check immediately after the sweep returned 403 even for root pages, so this is likely QRATOR/rate/session protection triggered by the rapid sweep rather than proof that every alias is invalid.
- Confirmed successful aliases: `design`, `programming`, `writing-translations`, `seo`, `promotion`, `audio-video`, `business`, `business-copywriting`, `creative-writing`, `interior-exterior-design`, `personal-assistant`, `presentations-infographics`, `usability-testing`, `analytics`, `animation`, `audio`, `audit`, `bulletin-boards`.
- High-value aliases found but not cleanly validated in that fast sweep because of 403: `links`, `script-programming`, `website-development`, `frontend`, `mobile-apps`, `smm`, `logo`, `traffic`, `information-bases`, `imagegeneration`, `textgeneration`, `videogeneration`, `server-administration`, `software`, `marketing`.

Important response-shape correction:

- In confirmed responses, `viewData.kworks` was a `dict`, not a simple list in the summary probe. PSR should not assume it is always `kworks[]` like the mobile `/kworks` API.
- `viewData.filters.activeCategoryId` mapped root/category aliases to numeric category ids; examples: `design -> 15`, `programming -> 11`, `writing-translations -> 5`, `seo -> 17`, `promotion -> 45`, `audio-video -> 7`, `business -> 83`, `bulletin-boards -> 112`, `audit -> 44`, `analytics -> 56`, `presentations-infographics -> 270`.

Autopentest prioritization:

- `prioritize_endpoints` ranked `POST https://kwork.ru/catalog_kworks_filters/{alias}` at score `24.5`, above `/projects`, `/kworks`, and `/getWantsCount`.
- Reason: many user-controlled catalog/filter parameters and direct web-catalog state.

PSR gap:

- Current PSR scanner uses `catalogMainv2 -> /kworks -> /projects/getWantsCount -> seller enrichment`.
- Current PSR does not call `/catalog_kworks_filters/{alias}` and does not parse web `stateData.viewData`.
- Recommended implementation: add a throttled web-catalog fallback/source that stores `alias`, `activeCategoryId`, filter state, card summary, raw key schema, HTTP status, and protection status. Use mobile `/kworks` as primary supply count and this endpoint as browser-real filter/card cross-check.

Implementation update:

- Added `KworkMarketClient.get_web_catalog_filters(alias, page=1, page_size=10, include_raw=False)` in `src/platforms/kwork_market.py`.
- Added compact parser `summarize_web_catalog_state(...)`; it handles the observed `viewData.kworks` dict shape and stores `protection_status`.
- Added backend route `GET /api/kwork/market/web-catalog/{alias}` with `page`, `page_size`, and `include_raw`.
- Added desktop API helper `api.getKworkWebCatalog(alias, options)`.
- Verification: `PYTHONPATH=C:\psr pytest tests/unit/test_kwork_market.py tests/unit/test_kwork_routes.py -q` -> `24 passed`.
- Verification: `npx vite build` in `desktop` -> passed.

## Web Catalog Collector Update - 2026-07-08 18:19 MSK

New implementation:

- `KworkMarketClient.get_web_catalog_filters(...)` now accepts runtime `cookies` for Session Hub-backed same-origin probes.
- Added `KworkMarketClient.get_web_catalog_alias_snapshot(...)` for throttled multi-alias collection.
- Added backend route `POST /api/kwork/market/web-catalog-snapshot`.
- Added desktop helper `api.getKworkWebCatalogSnapshot(...)`.
- Snapshot output includes `aggregate.status_counts`, `ok_count`, `blocked_count`, `empty_or_invalid_alias_count`, and optional `file_path`.
- Cookie values are not stored; only `cookie_count` is stored.

Live smoke:

- Artifact: `docs/kwork_web_catalog_snapshots/kwork_web_catalog_alias_snapshot_20260708T151918Z.json`.
- Session Hub returned 17 cookies.
- Aliases tested with `delay_seconds=2`: `programming`, `design`.
- Both returned HTTP 403 HTML, 1726 bytes, `protection_status=blocked`.
- The block page text says access was blocked due to high load from this side, likely caused by automated scripts.
- `autopentest_ai.identify_waf` did not match a known WAF vendor from the QRATOR/header/body sample.

Interpretation:

- Do not treat current 403 responses as empty catalog or invalid alias.
- The earlier successful artifacts remain valid evidence that `/catalog_kworks_filters/{alias}` works.
- The new PSR collector correctly preserves the difference between `ok`, `blocked`, `http_error`, `parse_error`, and `empty_or_invalid_alias`.

Verification:

- `PYTHONPATH=C:\psr pytest tests/unit/test_kwork_market.py tests/unit/test_kwork_routes.py -q` -> `26 passed`.
- `npx vite build` in `desktop` -> passed.

## Mobile Misc Endpoint Probe - 2026-07-08 18:24 MSK

Purpose: check remaining read-like mobile endpoints already wrapped in PSR/KworkExtensions and decide whether they help whole-market intelligence.

Artifact:

- `docs/kwork_mobile_misc_probe_2026-07-08T152410Z.json`

Sanitization:

- The artifact stores endpoint names, params, status/error type, and redacted error text only.
- Any IP-like value from remote error payloads was replaced with `<redacted_ip>`.

Endpoints checked:

- `POST /exchangeInfo`
- `POST /getCurrentVersions`
- `POST /getCaptchaStatus`
- `POST /getBadgesInfo`
- `POST /kworksStatusList`
- `POST /getUserInfo` with `id=3280`
- `POST /ordersBetween` with `user_id=3280`

Results:

- `exchangeInfo`, `getCurrentVersions`, `getCaptchaStatus`, and `getBadgesInfo` currently returned HTTP 403 at the mobile/API layer.
- `kworksStatusList` and `ordersBetween` returned parameter errors in the current cookie-only/auth-blocked state.
- `getUserInfo` still returned an unknown API error with simple `id` param.

Interpretation:

- These endpoints are not better than the already validated market path right now.
- For whole-market analysis, continue prioritizing `/projects`, `/getWantsCount`, `/kworks`, `/catalogMainv2`, `/catalogFilters`, `userByUsername`, `userKworks`, and kwork detail/review endpoints.
- `exchangeInfo` remains a candidate for market-level stats if token auth recovers, but current live evidence is blocked.
- `ordersBetween` is not a whole-market endpoint; it is account-specific relationship history and should not be part of broad market popularity scoring.

## VPNTE Proxy Rotation Note - 2026-07-08 18:42 MSK

Purpose: document how to use the user's local external proxy before continuing Kwork API research.

Reference:

- `docs/kwork_proxy_rotation_notes.md`

Live validation:

- PSR already has a VPNTE helper in `src/utils/vpnte_proxy.py`.
- Data path remains a stable local proxy on port `17990`.
- Control path uses the local VPNTE control API on port `17873` or the endpoint file under `%APPDATA%\VPN Tunnel Enforcer`.
- One rotation was tested successfully: `polandvless3` -> `norwayvless1`.
- A lightweight external request through the proxy succeeded after rotation.
- Token and IP-like values were not stored in this report.

Operational rule:

- Rotate only between small Kwork research batches or after block/protection signals.
- Validate `/status` and one lightweight proxy request after rotation.
- Keep Session Hub localhost calls isolated from proxy routing.

## Widget and Portfolio Popup Probe - 2026-07-08 18:48 MSK

Purpose: check a Tavily-discovered public widget endpoint and resolve remaining `/portfolio/get_popup` ambiguity.

Artifacts:

- `docs/kwork_widget_portfolio_probe_2026-07-08T154649Z.json`
- `docs/kwork_widget_portfolio_followup_2026-07-08T154755Z.json`

Context:

- Read-like probe batch only.
- Used current VPNTE local proxy on port `17990`.
- Session Hub returned 14 Kwork cookie names; cookie values were not stored.
- WSTG tracking: `WSTG-INFO-06`.

Results:

- `GET /api/widget/get` returned HTTP 302. `Location` strips `ref` and keeps widget params. Current shape is not useful for whole-market intelligence.
- `GET /portfolio_large/1123924` and `GET /portfolio_large/14040541` still return compact HTML portfolio detail loaders with `portfolio`, `kwork`, and `control-overlay` markers.
- `POST /portfolio/get_popup` with `portfolioId` returns HTTP 404 JSON page-not-found.
- `POST /portfolio/get_popup` with `portfolio_id` or `id` returns HTTP 200 JSON `success=true`, but `data.portfolio=null`; `data.additional` contains category taxonomy, `kworks=[]`, `anotherVideos=[]`, `hasOrders=false`.

Interpretation:

- Keep `/portfolio_large/{portfolio_id}` as the practical competitor portfolio-detail endpoint.
- `/portfolio/get_popup` is confirmed method/parameter-sensitive, but currently useful mostly for portfolio category taxonomy, not competitor portfolio content.
- `/api/widget/get` should stay low priority for market scanning.

## Kwork Sites and First Portfolio Probe - 2026-07-08 18:56 MSK

Purpose: validate two JS-only read-like endpoints that can enrich competitor kwork/portfolio analysis.

Artifacts:

- `docs/kwork_kworksites_firstportfolio_probe_2026-07-08T155357Z.json`
- `docs/kwork_firstportfolio_followup_2026-07-08T155534Z.json`

Source context:

- CDN `general` JS calls `/api/kwork/getkworksites` with `kworkId`, `showHosts`, `offset`.
- CDN `portfolio-view-popup` JS calls `/kwork_first_portfolio/{id}` via POST with `isWebpAccepted`.
- WSTG tracking: `WSTG-APIT-02`.

Results:

- `GET /api/kwork/getkworksites?kworkId=...&showHosts=...&offset=0` returned HTTP 200 JSON `success=true` with keys `html`, `linksSites`, `orderedLinksSites`, `success`; tested ids returned empty `linksSites` and empty `html`.
- `POST /kwork_first_portfolio/{id}` returned HTTP 404 for ids that were not valid kwork-with-portfolio ids in this context.
- Follow-up with kwork ids already known to have mobile `getKworkPortfolios` data (`36214125`, `42207254`, `14100522`, `43576198`, `21805649`) returned HTTP 200 HTML for 5/5. Response markers: `portfolio`, `kwork`, `control-overlay`, `content-item`, `portfolio-large`.
- Minimal REST API documentation discovery paths returned HTTP 404 on `kwork.ru` and HTTP 401 on `api.kwork.ru`; no public OpenAPI/Swagger document found in this pass.

Interpretation:

- `/kwork_first_portfolio/{kwork_id}` is useful for fast competitor portfolio enrichment when PSR already has a kwork id and knows/infers that the kwork has portfolio examples.
- `/api/kwork/getkworksites` is a conditional enrichment endpoint for kwork site/link rows, but not a broad market scanner source by itself.
- Structured portfolio collection should still prefer mobile `getKworkPortfolios`; web `/kwork_first_portfolio/{kwork_id}` is a direct HTML preview helper.

## portfolioList and userReviews Resolution - 2026-07-08 19:03 MSK

Purpose: resolve two previously incomplete seller/competitor endpoints by using upstream OpenAPI plus a focused authenticated probe after proxy rotation.

Artifact:

- `docs/kwork_portfoliolist_userreviews_probe_2026-07-08T160244Z.json`

Source of truth:

- Upstream `https://github.com/kesha1225/kwork` `docs/openapi.json` on branch `master`.
- Local installed `kwork-0.2.0` `openapi_mixin.py`.

Proxy/auth:

- VPNTE rotated before probe: `germanyvless1` to `kazakhstan1`.
- Mobile token was acquired in memory from local `.env`; token/password were not stored.
- WSTG tracking: `WSTG-APIT-02`.

Resolved signatures:

- `POST https://api.kwork.ru/portfolioList`
  - Auth: basic.
  - Required params: `user_id`, `category_id`, `page`.
  - Use `category_id=all` for all portfolio items.
- `POST https://api.kwork.ru/userReviews`
  - Auth: token+basic.
  - Required params: `token`, `user_id`, `type`.
  - Optional param: `page`.
  - `type` enum: `all`, `positive`, `negative`.

Live results:

- `portfolioList user_id=3280 category_id=all page=1`: HTTP 200, 12 portfolio items, `paging.total=30`, `pages=3`.
- `portfolioList user_id=9628615 category_id=all page=1`: HTTP 200, 12 portfolio items, `paging.total=254`, `pages=22`.
- `portfolioList user_id=9628615 category_id=15 page=1`: HTTP 200 valid empty category-filtered result.
- `userReviews user_id=3280 type=all page=1`: HTTP 200, 30 reviews, `paging.total=1761`, `pages=59`.
- `userReviews user_id=3280 type=positive page=1`: HTTP 200, 30 reviews, `paging.total=1743`.
- `userReviews user_id=3280 type=negative page=1`: HTTP 200, 18 reviews, `paging.total=18`.

Interpretation:

- `portfolioList` should replace the previous `userByUsername.portfolio_list` fallback when PSR needs paginated seller portfolio inventory by user id.
- `userReviews` should be used in authenticated scans for seller-level review history and positive/negative reputation split.
- Keep no-token fallbacks for unauthenticated runs: `userByUsername.reviews` and kwork-level `getKworkReviews`.

## Public Mobile-Basic Market Signal Probe - 2026-07-08 19:13 MSK

Purpose: validate additional upstream OpenAPI endpoints that may help whole-market supply and keyword intelligence without browser HTML.

Artifacts:

- `docs/kwork_public_market_signal_probe_2026-07-08T160926Z.json`
- `docs/kwork_public_market_signal_probe_basic_2026-07-08T161101Z.json`
- `docs/kwork_public_market_signal_probe_mobilebasic_2026-07-08T161227Z.json`
- `docs/kwork_search_signal_ru_probe_2026-07-08T161307Z.json`

Source/auth context:

- Tavily found the upstream Python `kwork` wrapper and JS `kwork-api` wrapper.
- Installed `kwork` package showed that `api.kwork.ru` expects the mobile API `Authorization` header.
- Plain unauthenticated calls and account login/password HTTP basic returned nginx HTTP 401.
- The same calls with the installed library's mobile basic header returned HTTP 200. No user password or token was written to artifacts.
- Proxy was still the rotated local VPNTE proxy on `127.0.0.1:17990`, profile `kazakhstan1`.

Results:

- `POST /search` with `query=telegram bot&limit=5&page=1`: HTTP 200, `success=true`, `kworks_count=5`, response keys `kworks_count`, `kworks`, `classifiers`, `searchCategories`, `paging`.
- `POST /search` with `query=telegram bot&categoryId=15&limit=5&page=1`: HTTP 200, `kworks_count=3`, category-scoped keyword search works.
- `POST /searchKworksCatalogQuery` with `query=telegram`: HTTP 200, 9 autocomplete rows with `suggestion` and `excerpt`.
- Russian probes `query=телеграм` and `query=логотип&categoryId=15` returned HTTP 200 but zero results in this mobile-basic context.
- `POST /getKworkLinksTable` and `POST /getKworkLinksTablev2` with `id=36214125&page=1`: HTTP 200 but empty link tables. Endpoints are live, but useful only for kworks that expose link/site rows.
- `POST /category` with `category_id=15`: HTTP 200 with compact category metadata: id/name/mobile description/base volume/volume type/attributes/positive_reviews_count.
- `POST /positiveReviewsCount` with `category_id=15` and with `attribute_id=208`: HTTP 200 but `response=false`; not yet proven useful as a market popularity signal.

Interpretation:

- `/search` is a real structured keyword supply endpoint and can complement `/kworks`, especially for English/transliterated terms. Do not replace classifier-based `/kworks` for Russian market scans until locale/query behavior is mapped.
- `/searchKworksCatalogQuery` is useful for keyword/autocomplete discovery, but zero-result language probes must be treated as query/locale behavior rather than endpoint failure.
- `/category` is a cheap metadata helper.
- `/getKworkLinksTable*` and `/positiveReviewsCount` remain conditional follow-up targets, not core scanner sources yet.

## Authenticated Demand API Probe - 2026-07-08 19:24 MSK

Purpose: validate fast demand/project endpoints for whole-market buyer-demand intelligence, using the same mobile API family PSR already uses for Kwork projects.

Artifact:

- `docs/kwork_demand_api_probe_direct_2026-07-08T162342Z.json`

Probe context:

- The longer PSR-service probe timed out before writing an artifact, so it is not used as evidence.
- The valid probe used installed `kwork.Kwork` directly with explicit local proxy `127.0.0.1:17990`, short timeout, and one retry.
- VPNTE status during the probe reported profile `usa1`, country `United States`.
- Kwork login/password from local environment were used only in memory; mobile token acquisition succeeded and token/password were not stored.
- WSTG context: `WSTG-INFO-06` and `WSTG-APIT-02`.

Live results:

- `POST /getWantsCount` with `categories=all`: success, `response.count=569`.
- `POST /projects` with `categories=all&page=1`: success, 12 project rows, `paging.total=569`, `limit=12`, `pages=48`.
- `POST /projects` with `categories=11&page=1`: success, `paging.total=164`, `pages=14`.
- `POST /projects` with `categories=all&page=1&query=telegram`: success, `paging.total=40`, `pages=4`.
- `POST /projects` with `categories=all&page=1&kworks_filter_to=5`: success, `paging.total=239`, `pages=20`.
- `POST /project` with a live project id `3213527`: success, card-like project detail.
- `POST /want` with the same id: success, richer want detail including date and view/order history fields.

Important response fields:

- `/projects` rows include `id`, `status`, `user_id`, `username`, `profile_picture`, `price`, `title`, `description`, `offers`, `time_left`, `parent_category_id`, `category_id`, `date_confirm`, `category_base_price`, `user_projects_count`, `user_hired_percent`, `user_active_projects_count`, `achievements_list`, `is_viewed`, `already_work`, `allow_higher_price`, `possible_price_limit`, `has_offer`, `user_need_kwork`, `user_need_portfolio`, `user_need_portfolio_rubric_name`.
- `/project` mirrors the project card/detail shape and is useful when PSR already has an id and needs the same buyer/project card fields.
- `/want` adds richer demand analytics fields: `date_create`, `date_active`, `date_expire`, `price_limit`, `views`, `orders`, `offers`, `views_history`, `allow_higher_price`, `possible_price_limit`.

Interpretation:

- `getWantsCount` is the cheapest demand-count primitive and should be used for category/query/filter sweeps.
- `projects` is the main paginated buyer-demand source and supports useful opportunity filters such as category, query, budget, hiring, and low-offer counts.
- `want` should be preferred over `project` for detailed market analytics because it exposes views and active/expiry history.
- These endpoints are high priority for PSR's whole-market opportunity scoring because they reveal what buyers are currently requesting and where competition is low.

## Metadata And Aggregate Market API Probe - 2026-07-08 19:31 MSK

Purpose: validate scalable category/filter/count endpoints that can drive market-wide seed generation without downloading every card.

Artifacts:

- `docs/kwork_metadata_aggregate_probe_2026-07-08T162920Z.json`
- `docs/kwork_catalog_filter_followup_2026-07-08T163032Z.json`

Probe context:

- VPNTE rotated from `usa1` to `usa2`; local proxy remained `127.0.0.1:17990`.
- API calls used the installed-library mobile basic header. No account token was needed for this metadata sweep.
- WSTG context: `WSTG-INFO-06` and `WSTG-APIT-02`.

Live results:

- `POST /categories`: HTTP 200, 7 root categories. Items include `id`, `name`, `description`, `subcategories`.
- `POST /category` for `category_id=11` and `15`: HTTP 200 compact metadata with `id`, `name`, `mobile_description`, `base_volume`, `volume_type`, `attributes`, `positive_reviews_count`.
- `POST /catalogMainv2`: HTTP 200, large catalog-home payload with `popular_categories_block`, `catalog_service_block`, `other_services_block`. Popular category cards include `category_id`, `classifier_id`, `name`, cover URLs, `kworks_count`, and `order`.
- `POST /catalogFilters` with camelCase `categoryId`:
  - `categoryId=11`: `kworks_count=103023`.
  - `categoryId=15`: `kworks_count=259863`.
  - `categoryId=25`: `kworks_count=46170`, 6 filters.
  - `categoryId=41`: `kworks_count=38135`, 6 filters.
- `POST /catalogFilters` with snake_case `category_id` is misleading here: for 11/25/41 it often returned generic `kworks_count=784454` and a different filter set.
- `POST /categoryAttributes` on root categories `11` and `15`: HTTP 200 but empty list.
- `POST /categoryAttributes` on child categories `25` and `41`: HTTP 200 list length 1, with classification tree rooted at `Тип`; fields include `is_classification`, `required`, `percent_usage`, `alias`, `children`, `meta_title`, `kworks_count`.
- `GET /api/freeprice/categorygetprices` and `/attributegetprices`: HTTP 200 `success=true`, but empty/null prices for tested category/attribute inputs.
- `POST /positiveReviewsCount`: HTTP 200 but `response=false` for tested inputs.

Interpretation:

- `catalogMainv2` is a high-value seed endpoint: it gives curated popular niches with `category_id`, `classifier_id`, `kworks_count`, imagery, and display order.
- `catalogFilters` must use `categoryId`, not `category_id`, when PSR needs real category-level supply counts and filter dimensions.
- `categoryAttributes` is useful on leaf/child categories, not root categories. It should drive deep classifier fan-out and UI/form mapping.
- Freeprice and positive-review aggregate endpoints remain conditional/low priority until inputs that return non-empty payloads are found.

## Seller And Competitor Detail Probe - 2026-07-08 19:37 MSK

Purpose: validate seller/profile endpoints around a real competitor card so PSR can enrich competitors without profile HTML.

Artifact:

- `docs/kwork_seller_competitor_probe_2026-07-08T163639Z.json`

Probe context:

- VPNTE rotated from `usa2` to `turkeyvless1`; status country during probe was `Germany`.
- Seed card came from `POST /kworks categoryId=41`: `kwork_id=255535`, worker `teleprog`, user id `198065`.
- Mobile token was acquired in memory for token-required endpoints; no token/password was stored.
- WSTG context: `WSTG-INFO-06` and `WSTG-APIT-02`.

Live results:

- `POST /userByUsername username=teleprog`: success. Returned broad seller profile with ratings, review counts, online/live date, custom-request budget, order completion percentages, achievements, completed order count, specialization/profession, embedded kworks, portfolio list, reviews, skills, verification flags.
- `POST /user id=198065`: success with the same broad profile shape.
- `POST /kworksCategoriesList user_id=198065`: success, 2 seller categories.
- `POST /userKworks user_id=198065&page=1` with token: success, 2 kwork cards, paging total 2.
- `POST /portfolioList user_id=198065&category_id=all&page=1`: success, 12 portfolio rows. Rows include portfolio ids, titles, category, media URLs, `views`, `views_dirty`, comments, images/videos/audio/pdf arrays.
- `POST /userReviews user_id=198065&type=all&page=1` with token: success, 30 rows, total 378, 13 pages.
- `POST /userReviews user_id=198065&type=negative&page=1` with token: success, 2 rows, total 2; negative rows can include seller answer.
- `POST /userSearch query=teleprog&page=1`: success, 2 lightweight user rows with id/username/profilepicture/rating/review counts/online status.
- `POST /getKworkDetailsExtra id=255535`: success. Returned `recommended_kworks`, `similar_kworks`, `other_kworks`, review aggregates, `last_reviews`, `reviews_count=121`, `kwork_ratings`, and FAQ count.
- `POST /getKworkAnswers id=255535`: success but `response=None` for this kwork.
- `POST /getKworkPortfolios id=255535&page=1`: success but empty list for this kwork.
- `POST /getKworkReviews kwork_id=255535&type=all&page=1`: success, 30 rows, total 121, 5 pages.

Interpretation:

- `userByUsername`/`user` are sufficient to build rich seller cards without profile HTML.
- `portfolioList` and `userReviews` should be added to PSR seller enrichment for authenticated scans; they are much richer than embedded profile snippets.
- `getKworkDetailsExtra` is a high-value competitor expansion endpoint because it exposes recommended/similar/other kworks around a specific competitor.
- `userSearch` is useful for lightweight seller lookup/autocomplete, but profile enrichment should still use `userByUsername` or `user`.
- `getKworkAnswers` and `getKworkPortfolios` are conditional per kwork; empty responses should be stored as `empty`, not treated as failures.

## Catalog Taxonomy Probe - 2026-07-08 20:25 MSK

Purpose: validate the older catalog taxonomy endpoints that were still isolated in the autopentest graph and determine whether they are useful for whole-market category discovery.

Artifacts:

- `docs/kwork_catalog_taxonomy_probe_2026-07-08T164313Z.json`
- `docs/kwork_catalog_main_retry_2026-07-08T164819Z.json`
- `docs/kwork_catalog_main_auth_probe_2026-07-08T172454Z.json`

Probe context:

- WSTG context: `WSTG-INFO-06` and `WSTG-APIT-02`.
- VPNTE retry rotated from `russia3` to `russia4`; proxy URL stayed `127.0.0.1:17990`.
- Public endpoints used the mobile basic header. The `catalogMain` follow-up used an account token in memory only.
- Tavily search found public wrappers and Kwork pages, but no reliable public documentation for these catalog taxonomy endpoints.

Live results:

- `POST /catalogRubrics` with no params: success, 7 root rubrics. Items include `id`, `name`, `rubric_description`, `order`, `category_image`, `ico`, `ico_extra`.
- `POST /catalogRubrics` with tested `categoryId`, `category_id`, `rubricId`, or `id`: success, but params were ignored and the same 7 root rubrics were returned.
- `POST /catalogCategories rubricId=11`: success, 9 child categories with counts. Examples: website setup `13664`, website creation `24193`, scripts/bots/mini apps `37914`, HTML/CSS layout `6509`, desktop programming `5201`.
- `POST /catalogCategories rubricId=15`: success, 11 child categories with counts. Examples: logo/branding `46246`, web/mobile design `17610`, art/illustration `28103`, print design `9794`.
- `POST /catalogCategories rubricId=5/7/83`: success for text/translation, audio/video, and business/life root rubrics.
- `POST /catalogCategories` with `rubric_id`, `categoryId`, `category_id`, `parentId`, or `id`: HTTP 200 but API `success=false`, error `Недостаточно параметров для метода API`.
- `POST /catalogMainv2` without token: success, response keys `popular_categories_block`, `catalog_service_block`, `other_services_block`. Other-services rows can include `category_id` and `classifier_id`.
- `POST /catalogMain` without token: HTTP 200 but API `success=false`, error `Необходима авторизация`.
- `POST /catalogMain` with account token: success, response keys `rubrics`, `popular_categories`, `popular_kworks`, `viewed_kworks`.

Interpretation:

- `catalogRubrics -> catalogCategories(rubricId)` is a cheap root-to-child taxonomy path with market-wide kwork counts. It should seed broad scans before deeper `categoryAttributes`, `catalogFilters`, and `/kworks` fan-out.
- `catalogMainv2` remains the public curated seed endpoint; it is useful even without auth and can provide classifier-level niches.
- Authenticated `catalogMain` is useful as a personalized market-home signal source, especially `popular_kworks` and `viewed_kworks`, but it should not replace public taxonomy endpoints.
- The strict parameter rule for `catalogCategories` is important: only camelCase `rubricId` produced useful data in this probe.

## Account And Misc Endpoint Probe - 2026-07-08 20:33 MSK

Purpose: validate another cluster of isolated endpoints around competitor details, own seller state, viewed catalog history, and web helper APIs.

Artifact:

- `docs/kwork_account_misc_endpoint_probe_2026-07-08T173238Z.json`

Probe context:

- WSTG context: `WSTG-INFO-06` and `WSTG-APIT-02`.
- VPNTE rotated from `russia4` to `russia5`; local proxy stayed `127.0.0.1:17990`.
- Mobile token was acquired in memory only; Session Hub supplied 14 web cookies.
- Seeds: competitor kwork `255535`, category `41`, project `3213556`.

Live results:

- `POST /getKworkDetails id=255535`: success. Rich competitor detail includes title, description, instructions, price, packages/options, queue/favorite counts, classifications, category/catalog metadata, media, portfolio item metadata, and seller short info.
- `POST /category category_id=41`: success. Compact metadata includes name, mobile description, base volume, volume type, attributes, and positive review count.
- `POST /getKworkPortfolios id=255535&page=1`: success with empty list for this kwork.
- `POST /actor` with token: success. Full current-account seller object including balances, notification counts, own kworks, portfolio/review snippets, worker status, wants/offers counts, and account flags.
- `POST /getActorInfo` with token: success. Compact current-account status object with totals, own kwork counts, worker status, offer counts, push/verification flags.
- `POST /offers page=1` with token: success. Top-level keys `success`, `response`, `connects`, `paging`; response list empty in this snapshot.
- `POST /kworksStatusList` with token: success. 8 status buckets; examples: active `1`, moderation `0`, requires fixes `0`.
- `POST /viewedCatalogKworks page=1` with token: success. 10 viewed catalog cards with category, status, title, share URL, image/cover, price, worker, badges, order, `classification_id`.
- `GET /api/user/checknotify` with web cookies: success, keys `success`, `data`.
- `POST /api/user/checkworkeroffers` with web cookies: success, keys `success`, `data`.
- `GET /api/kwork/getkworksites?kworkId=255535` with web cookies: success, keys `success`, `linksSites`, `orderedLinksSites`, `html`.
- `POST /projects/check_is_template` with web cookies and JSON `description`/`wantid`: success, keys `success`, `data`.
- `GET /wants/{wantId}/check_offer_notify` with web cookies: success, keys `success`, `data`.
- `POST /api/offer/addview` with web cookies and tested `project_id`: HTTP 200 JSON key `result`; endpoint is live but market value remains unproven.

Interpretation:

- `getKworkDetails` is now fully confirmed as the fast structured replacement for HTML competitor detail loading.
- `actor`, `getActorInfo`, `kworksStatusList`, and `offers` are account-state endpoints for PSR seller dashboards, health checks, own kwork state, and connects/proposal state.
- `viewedCatalogKworks` is useful as personalized competitor/interest history, but it should be stored separately from whole-market rankings because it is account-biased.
- Web helper endpoints are operational helpers for proposals/notifications/link snippets. They are live, but only `getkworksites` may become useful for niche analysis of link/SEO kworks.

## Remaining Isolated Endpoint Probe - 2026-07-08 20:40 MSK

Purpose: validate/link the last isolated freeprice and portfolio form endpoints without calling mutating analytics counters.

Artifact:

- `docs/kwork_remaining_isolated_probe_2026-07-08T174026Z.json`

Probe context:

- WSTG context: `WSTG-INFO-06` and `WSTG-APIT-02`.
- VPNTE rotated from `russia5` to `russiavless2`; local proxy stayed `127.0.0.1:17990`.
- Mobile token was acquired in memory; Session Hub supplied 14 cookies.
- Own kwork seed from `/actor`: `51594444`, category `41`.

Live results:

- `GET /api/freeprice/categorygetprices categoryId=11/15&lang=ru`: success but `prices=null` for root categories.
- `GET /api/freeprice/categorygetprices categoryId=41&lang=ru`: success with useful price rules. Keys: `priceGradation`, `minPrice`, `maxPrice`, `typicalPriceGradation`, `predefinedPackageItem`. For category 41: `minPrice=500`, `maxPrice=71183`, `priceStandard` has 42 values from `500` to `60000`, and `typicalPriceGradation` goes up to `70000`.
- `GET /api/freeprice/attributegetprices` for tested `(categoryId=11, attributeId=208)` and `(categoryId=41, attributeId=3587)`: success but `prices=null`.
- `POST /portfolio/load_form_orders kworkId=51594444` with web cookies: success, `anotherVideos=[]`, `hasOrders=false`.
- `POST /portfolio/load_form_orders_full kworkIds=51594444` with web cookies: success, `orders=[]`.
- `POST /portfolio/load_form_kworks categoryId=41&attributesId=3587` with web cookies: success, `kworks=[]`, `hasOrders=false`.
- `POST /portfolio/log_portfolio_view`: not live-probed because JS context marks it as a view analytics endpoint with params `portfolio_video_id`, `view_type`.

Interpretation:

- `categorygetprices` is useful for leaf categories and should feed price controls/package suggestions when `prices` is non-null.
- `attributegetprices` stays conditional until non-null attribute inputs are found.
- Portfolio form loaders are publishing/account helpers, not whole-market market-data sources.
- `log_portfolio_view` should remain documented but should not be called during research scans.

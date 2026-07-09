# Kwork Hidden API Facts

Дата: 2026-07-08

Только проверенные факты и полезные находки. Секретов, cookies, токенов и паролей здесь нет.

## Mobile API host

Base: `https://api.kwork.ru/{endpoint}`

Без mobile Authorization header host отвечает `401 Unauthorized` с `WWW-Authenticate: Basic realm="Authentication Required!"`. Для endpoint-ов `auth: token+basic` библиотека получает token через `POST /signIn`.

## Market

| Endpoint | Auth | Params | Fact |
|---|---|---|---|
| `POST /categories` | basic | none | Вернул 7 root categories with `subcategories`. |
| `POST /kworks` | basic | `categoryId`, `classifierId`, `page` | `categoryId=41` дал `kworks_count=38124`, 10 kworks, 8 classifiers. |
| `POST /kworks` | basic | `categoryId=41&classifierId=3587&page=1` | Дал `kworks_count=18261`, first titles are Telegram/chatbot relevant. |
| `POST /kworks` | basic | `categoryId=41&attribute[208]=3587&page=1` | Count остался `38124`; `attribute[...]` не сузил выдачу как `classifierId`. |
| `POST /categoryAttributes` | basic | `category_id` | Вернул tree attributes, children, `allow_multiple`, counts/usage. |
| `POST /catalogFilters` | basic | `categoryId` | Вернул `filters`, `groups`, `kworks_count`; filter items have `query_key`, `values`, min/max. |
| `POST /searchKworksCatalogQuery` | basic | `query` | `telegram` дал 9 suggestions with `suggestion`, `excerpt`. |
| `POST /positiveReviewsCount` | basic | `category_id` | Для `41` вернул thresholds `1`, `5`, `10`, `20`, `50`, `100`. |

## Kwork detail / competitor analysis

| Endpoint | Auth | Params | Fact |
|---|---|---|---|
| `POST /getKworkDetails` | basic | `id` | Возвращает title, description, instructions, price, term, user info, packages, portfolio. |
| `POST /getKworkDetailsExtra` | basic | `id` | Возвращает reviews counters, last reviews, ratings, similar/recommended/other kworks. |
| `POST /getKworkReviews` | basic | `kwork_id`, `page` | Вернул reviews list + paging. |
| `POST /getKworkAnswers` | basic | `id` | Вернул FAQ list: `question`, `answer`, `position`. |
| `POST /getKworkPortfolios` | basic | `id`, `page` | Работает, возвращает `response[]` + `paging`; sample kwork had empty list. |
| `POST /getKworkLinksTable` | basic | `id` | Работает, возвращает `response[]` + `paging`; sample kwork had empty list. |

## Demand / orders / wants

| Endpoint | Auth | Params | Fact |
|---|---|---|---|
| `POST /projects` | token+basic | `categories`, `page`, `query`, `price_from`, `price_to`, `hiring_from`, `kworks_filter_from`, `kworks_filter_to` | `categories=all&page=1` вернул 12 wants + `paging` + `connects`. |
| `POST /projects` | token+basic | canonical filters vs web UI filters | Live matrix 2026-07-09: `query`, `price_from`, `kworks_filter_to` work; web fields `keyword`, `kworks_filters`, `prices_filters` were ignored and matched baseline counts. |
| `POST /getWantsCount` | token+basic | `categories`, filters | `categories=all` вернул `response.count=534`. |
| `POST /project` | token+basic | `id` | Для want `3213259` вернул detailed project dict. |
| `POST /want` | token+basic | `id` | Для want `3213259` вернул list length 1 with `views`, `views_history`, `orders`, dates. |
| `POST /wantsStatusList` | token+basic | optional filters | Работает; на аккаунте sample response length 0. |
| `POST /myWants` | token+basic | paging | Работает; sample response length 0, paging `{page,total,limit,pages}`. |
| `POST /offers` | token+basic | paging | Работает; sample response length 0, top-level `connects.active_connects=30`. |

## Account / analytics

| Endpoint | Auth | Params | Fact |
|---|---|---|---|
| `POST /actor` | token+basic | none | Возвращает current user, balances, counts, own kworks, offers/wants counts, notifications. |
| `POST /getCaptchaStatus` | token+basic | none | Cookie-only Session Hub probe on 2026-07-09 raised `KworkException: Некорректные значения параметров`; treat this endpoint as an API diagnostic only, not proof of a visible web SmartCaptcha. |
| `POST /getActorInfo` | token+basic | none | Compact current user/account info. |
| `POST /kworksStatusList` | token+basic | none | Вернул 8 tabs with `id`, `name`, `kworks_count`, `kworks`. |
| `POST /viewedCatalogKworks` | token+basic | paging | Вернул viewed kworks list; sample total 2. |
| `POST /favoriteKworks` | token+basic | paging | Работает; sample empty list. |
| `POST /userByUsername` | basic | `username` | Возвращает public profile, stats, kworks, portfolio, skills, custom request data. |
| `POST /userSearch` | basic | `query` | `grandlers` вернул 1 user with id/rating/reviews_count. |

## Web form / pricing APIs

Base: `https://kwork.ru`

| Endpoint | Auth | Params | Fact |
|---|---|---|---|
| `GET /api/attribute/loadclassification` | web/public | `categoryId`, `lang`, optional `attributeId`, `value` | Возвращает JSON `success/html`; HTML содержит real inputs like `attribute[208]`, `attribute[211]`. |
| `GET /api/freeprice/categorygetprices` | public | `categoryId`, `lang` | Для `41` вернул price gradations, `minPrice=500`, `maxPrice=71183`. |
| `GET /api/freeprice/attributegetprices` | public | `categoryId`, `attributeId`, `lang` | Для `41/208` вернул `success=true`, `prices=null`. |

## Authenticated web state

| Source | Auth | Fact |
|---|---|---|
| `GET /projects` | Session Hub web cookies | HTML содержит `window.stateData` with 218 top keys. |
| `stateData.exchangeStats` | Session Hub web cookies | `{projects: 8623, orders: 4063, value: 89836000, period: 30}`. |
| `stateData.connect` | Session Hub web cookies | `connect_points=30`, `accrued_connect_points=30`, `isPenalised=false`. |
| `stateData.wantsListData.wants` | Session Hub web cookies | 12 active wants in page state with descriptions, prices, categories, buyer stats. |
| `stateData.wantsListData.filters` | Session Hub web cookies | Budget and kwork-count filter boundaries. |
| `stateData.wantsListData.favouriteCategories` | Session Hub web cookies | Category `41` includes attributes `211`, `3587`, `7352`, `3934090`, `4158112`, `4158119`, `5503256`, `5548694`. |

## JS-discovered web endpoints

These were found in Kwork JS bundles. Some are state-changing; facts below only state discovery or safe response.

| Endpoint | Method | Fact |
|---|---|---|
| `/api/order/getorderscount` | POST | With Session Hub cookies returned `{asPayer, hasPayerImportant, asWorker, payerWants}`; without web session returned `false`. |
| `/api/user/checknotify` | GET | Returned notification counts and dialog notice data. |
| `/api/user/checkworkeroffers` | POST | Returned `success=true`, `data.has_offers=false`. |
| `/api/user/getworkbaysellers` | GET | Returned `success=true`, `data=null`. |
| `/api/kwork/getkworksites` | GET | Params `kworkId`, `showHosts`, `offset`; returned `linksSites`, `orderedLinksSites`, `html`. |
| `/api/offer/addview` | POST | Found in projects JS; marks want cards viewed. |
| `/api/offer/addview` | POST | With Session Hub cookies and `wantIds[]=3213259` returned `{"result": true}`. |
| `/wants/{wantId}/check_offer_notify` | GET | With Session Hub cookies for `3213259` returned `categoryId=41`, `parentCategoryId=11`, `classificationId=7352`, portfolio/kwork notification flags and `urlForCreatePortfolio`. Useful before offer generation. |
| `/projects/check_is_template` | POST | With Session Hub cookies and draft offer text returned `{"success": true, "data": []}`. Useful as template/spam preflight before creating offer. |
| `/wants/portfolio` | POST | Found in `new-offer` JS with JSON `{userId, category, page, lang}`. Live Session Hub probe for `project=3202311` returned HTTP 200 JSON `success=true`, keys `totalCount`, `offset`, `curCount`, `haveNext`, `portfolioJson`, `allPortfolioIds`, empty for tested account/category. Offer-form portfolio helper, not a buyer discovery feed. Artifacts: `docs/kwork_market_snapshots/kwork_wants_portfolio_probe_20260709T090535Z.json`, `docs/kwork_market_snapshots/kwork_wants_portfolio_actor_probe_20260709T090758Z.json`. |
| `/wants/hide_want` | POST | Found in projects JS; state-changing. |
| `/projects/manage/restart` | POST | Found in projects JS; state-changing. |
| `/wants/review/save` | POST | Found in projects JS; state-changing. |
| `/api/file/upload` | POST | Found in projects JS; upload. |
| `/api/kwork/deleteattachment` | POST | Found in projects JS; state-changing. |
| `/api/portfolio/youtubelinkvalidate?url=` | GET | Found in projects JS; URL validation. |
| `/portfolio/load_form_orders` | GET/POST candidate | Found in projects JS. |
| `/portfolio/load_form_orders_full` | GET/POST candidate | Found in projects JS. |
| `/portfolio/load_form_kworks` | GET/POST candidate | Found in projects JS. |

## Key rules

- For competitor relevance, `classifierId` works; `attribute[...]` did not narrow `/kworks` in confirmed tests.
- For orders/demand, prefer `POST /projects` and `POST /getWantsCount`.
- For exact publish form controls, use `GET /api/attribute/loadclassification`.
- For UI-auth reality, keep Session Hub running and inspect `GET /projects` `window.stateData`.
- For competitor detail, use `getKworkDetails` plus `getKworkDetailsExtra` instead of HTML `share_url`.

## 2026-07-08 live facts addendum

No secrets, cookies, tokens, Authorization headers or password values are stored here.

### Whole-market supply facts

| Endpoint | Auth | Params | Fact |
|---|---|---|---|
| `POST /categories` | basic | none | Live check returned 7 root categories and 61 total category nodes. |
| `POST /kworks` | basic | `categoryId=41` | Live check returned `kworks_count=38151`; top classifiers included `211=2900`, `3587=16769`, `7352=7555`, `3934090=1838`, `5548694=991`, `4158112=2921`. |
| `POST /kworks` | basic | `categoryId=59` | Live check returned `kworks_count=45600`; top link-building classifiers included `1379472=19115`, `1379027=6854`, `1378848=10212`. |
| `POST /kworks` | basic | `categoryId=56` | Live check returned `kworks_count=2148`; `943` (site/market analysis) had `1477` kworks. |
| `POST /kworks` | basic | `categoryId=44` | Live check returned `kworks_count=2387`; `1120` (SEO audit) had `1479` kworks. |
| `POST /kworks` | basic | `categoryId=72` | Live check returned `kworks_count=3101`; traffic/behavior slices were `199=2629`, `200=577`. |

### Whole-market demand facts

| Source | Auth | Fact |
|---|---|---|
| `GET /projects` | Session Hub web cookies | Live check returned HTTP 200, HTML length about `309847`, and `window.stateData` with 218 top-level keys. |
| `stateData.exchangeStats` | Session Hub web cookies | Live value: `projects=8623`, `orders=4063`, `value=89836000`, `period=30`. |
| `stateData.wantsListData.wants` | Session Hub web cookies | Live page contained 12 want cards. Titles included AI/audio automation, AI task setup, AI finance agent, Telegram bot tuning, AI content factory, comment automation. |
| `stateData.wantsListData.attributesCount` | Session Hub web cookies | Live top attributes: `7352=16`, `3587=11`, `5548694=8`, `1466231=8`, `4158112=8`, `211=6`, `1357=5`. |
| `POST /projects` | token+basic | With explicit `.env` load and PSR `KworkService`, `categories=all&page=1` returned 12 projects plus `connects` and `paging`; `categories=41&page=1` also returned 12 projects. If a standalone script skips `.env`, it may fall back to Session Hub `cookie-only` and return parameter errors. |
| `POST /getWantsCount` | token+basic | With explicit `.env` load and PSR `KworkService`, `categories=all` returned count `530`. Use `/projects` `window.stateData` as fallback/cross-check. |

### Competitor profile facts

| Endpoint | Auth | Params | Fact |
|---|---|---|---|
| `POST /userByUsername` | basic | `username=MDevostator` | Returned `id=1354`, `rating=5.0`, `reviews_count=1085`, `kworks_count=89`, `completed_orders_count=2864`, repeat `76%`, skills `11`. |
| `POST /userByUsername` | basic | `username=Nekrashevich_Alex` | Returned `id=3280`, `rating=5.0`, `reviews_count=1761`, `kworks_count=89`, `completed_orders_count=2133`, portfolio `12`, skills `12`. |
| `POST /userByUsername` | basic | `username=ProxyCorp` | Returned `id=3194382`, `rating=5.0`, `reviews_count=538`, `kworks_count=4`, `completed_orders_count=972`, repeat `59%`. |
| `POST /userByUsername` | basic | `username=leaflex` | Returned `id=598386`, `rating=4.9`, `reviews_count=814`, `kworks_count=20`, `completed_orders_count=2752`, repeat `68%`. |

### New JS-discovered endpoints from latest extraction

Fetched `/`, `/projects`, `/categories/programming`, `/categories/seo`; found 36 unique Kwork/CDN JS bundles; 6 bundles contained endpoint literals.

| Endpoint | Method | Fact |
|---|---|---|
| `/projects/list/{username}` | GET | JS builds it as `"/projects/list/" + wantData.user.username`. Live checks for `Evdohaanna2403`, `smm_vk`, `grandlers` returned HTTP 200 HTML with `stateData` 194 keys but `wants_len=0`. Buyer-specific page, not primary whole-market feed. |
| `/portfolio/load_form_orders` | GET/POST candidate | Found in `projects-list-dcl` JS. Useful for portfolio/order binding data. Needs validation. |
| `/portfolio/load_form_orders_full` | GET/POST candidate | Found in `projects-list-dcl` JS. Useful for full order list in portfolio flow. Needs validation. |
| `/portfolio/load_form_kworks` | GET/POST candidate | Found in `projects-list-dcl` JS. Useful for seller kwork list in portfolio flow. Needs validation. |
| `/portfolio/get_popup` | POST candidate | Found in multiple JS bundles. Candidate for portfolio detail popup. Needs validation. |
| `/portfolio/can_delete/` | GET candidate | Found in JS. State/control endpoint, not primary for read-only market scan. |
| `/api/user/setworkerstatusswitchall` | POST candidate | Found in header JS. State-changing seller status endpoint. |
| `/api/user/switchworkerstatus` | POST candidate | Found in header JS. State-changing seller status endpoint. |

### Practical PSR rules from current facts

- Supply: walk `/categories`, then `/kworks` by category and top `classifierId`.
- Demand: prefer token `/projects` + `/getWantsCount` with `.env` loaded; if token is absent and Session Hub is cookie-only, parse `/projects` `window.stateData`.
- Competitors: enrich top cards with `userByUsername`, `userKworks`, `getKworkDetails`, `getKworkDetailsExtra`, `getKworkReviews`, `getKworkAnswers`.
- Market trend scoring needs persistence. Current PSR can see a point-in-time slice, not growth/decay without snapshots.

## Additional validated mobile catalog facts

| Endpoint | Auth | Params | Fact |
|---|---|---|---|
| `POST /catalogMainv2` | basic | none | Returned `popular_categories_block`, `catalog_service_block`, `other_services_block`. `popular_categories_block` had 3 sections and 74 category/classifier items with `category_id`, `classifier_id`, `cover_url`, `kworks_count`. |
| `POST /catalogMainv2` | basic | none | Top live items by supply: Marketplace design `82696`, Links `45547`, Ready databases `34073`, Logos `33636`, Marketplaces `21114`, Illustrations `18150`, Presentations `11605`, Telegram `7157`, AI logos/infographics `6642`. |
| `POST /catalogRubrics` | basic | none | Returned 7 rubrics with `id`, `name`, `order`, `rubric_description`, image fields. |
| `POST /catalogCategories` | basic | `rubricId` | `rubricId=11` returned 9 categories. Important: camelCase `rubricId` works; `rubric_id`, `categoryId`, `category_id`, `parentId`, `id` did not. |
| `POST /kworksCategoriesList` | basic | `user_id` | `user_id=3280` returned 13 seller category tabs with `id`, `name`, `kworks_count`, `kworks`. |
| `POST /userKworks` | basic | `user_id`, `page` | `user_id=3280&page=1` returned 10 kwork cards plus `paging`; fields include `id`, `title`, `price`, `category_id`, `category_name`, `cover`, `share_url`, `worker`, `badges`, status fields. |
| `POST /portfolioList` | basic per library | unknown | Tried `user_id`, `userId`, `username`, `worker_id`, `seller_id`, `id`, `type`, `portfolio_type`, `category_id`, `categoryId`; not enough/invalid params. Use `userByUsername.portfolio_list` until params are found. |
| `POST /userReviews` | token+basic per library | unknown | Tried `user_id`, `userId`, `id`, `username`, `type`, `review_type`, `filter`; not enough/invalid params. Use `userByUsername.reviews` until params are found. |

## 2026-07-08 16:20 MSK additional facts

Snapshot artifact: `docs/kwork_api_probe_snapshot_2026-07-08.json`. It stores response shapes and samples only; no secrets, tokens, cookies, Authorization headers, or password values.

### Additional mobile/catalog endpoints

| Endpoint | Auth | Working params | Fact |
|---|---|---|---|
| `POST /catalogMain` | token+basic | none observed | Returned `response.rubrics`, `response.popular_categories`, `response.popular_kworks`, `response.viewed_kworks`; live counts were 7, 4, 20, 3. Useful for personalized popular catalog/kwork samples. |
| `POST /catalogMainv2` | basic | none observed | Correct recursive parse found 84 category/classifier/service items. `popular_categories_block` is nested under themed section objects with `categories[]`. |
| `POST /catalogRubrics` | basic | none observed | Returned 7 live rubrics. |
| `POST /catalogCategories` | basic | `rubricId` | Calling all live rubric ids returned 54 category records total. Use together with `/categories` because `/categories` returned 61 nodes. |
| `POST /catalogFilters` | basic | `categoryId` | Categories `41`, `59`, `56`, `44`, `72`, `113`, `286`, `306` returned `response.filters`, `response.groups`, `response.kworks_count`. |
| `POST /category` | basic | `category_id` | `category_id=41/59/286` returned category dict with `id`, `name`, `mobile_description`, `base_volume`, `volume_type`, `attributes`, `positive_reviews_count`. `id`, `categoryId`, `catId`, `rubricId` did not work. |
| `POST /positiveReviewsCount` | basic | `category_id` | `category_id=41/59/286` returned dict keys such as `1`, `5`, `10`, `20`, `50`, `100`. `categoryId`, `id`, `categoryId+classifierId` returned `response=false`. |
| `POST /searchKworksCatalogQuery` | basic | `query` | `query=telegram`, `seo`, `python` returned 9 suggestions each in live probe. |

### Additional seller/competitor endpoints

| Endpoint | Auth | Working params | Fact |
|---|---|---|---|
| `POST /user` | basic | `id` | `id=3280` returned public user profile fields: `id`, `username`, `status`, `fullname`, `profilepicture`, `description`, `slogan`, `location`, `rating`, `rating_count`, `level_description`, `good_reviews`. |
| `POST /userSearch` | basic | `query` | `query=Nekrashevich` returned 31 users plus `paging`. Useful for finding competitors by partial username/name. |
| `POST /viewedCatalogKworks` | token+basic | none/page optional | Live account sample returned 3 viewed kworks plus `paging`; personal history signal, not whole-market signal. |
| `POST /getKworkPortfolios` | basic | `id`, optional `page` | `id=29072946` returned `success=true`, empty `response[]`, and `paging`. `kwork_id` and `kworkId` did not work. |

### Corrected top `catalogMainv2` supply seeds

| Name | category_id | classifier_id | kworks_count |
|---|---:|---:|---:|
| Marketplace design | 286 | 1433413 | 82696 |
| Links | 59 | | 45547 |
| Ready databases | 113 | 1117 | 34073 |
| Logos | 25 | 401928 | 33636 |
| Marketplaces | 112 | 1357 | 21114 |
| Illustrations and drawings | 28 | 819 | 18150 |
| Presentations | 270 | 314398 | 11605 |
| Photo editing | 68 | 399860 | 11495 |
| Web design | 24 | 393348 | 11331 |
| Social media design | 286 | 1433356 | 8304 |

### Still unresolved / fallback required

| Endpoint | Status | Fallback |
|---|---|---|
| `POST /portfolioList` | Simple params `user_id`, `userId`, `id`, `username`, `login`, `worker_id`, with/without `page`, returned "not enough parameters". | Use `userByUsername.portfolio_list` and web `/portfolio/load_form_*` loaders. |
| `POST /userReviews` | Simple params `user_id`, `userId`, `id`, `username`, `login`, `worker_id`, with/without `page`, returned "not enough parameters". | Use `userByUsername.reviews` and kwork-level `getKworkReviews`. |
| `POST /getUserInfo` | Simple user params returned invalid/unknown API error. | Use `POST /user` by id and `POST /userByUsername` by username. |

### Tavily/public map fact

Tavily map for `kwork.ru` with path filters for `/categories`, `/projects`, `/users`, `/kwork`, `/portfolio`, `/api` returned only the 7 public top category URLs. No additional public API-like path was found in this pass.

## 2026-07-08 16:50 MSK demand and competitor facts

Artifacts:

- `docs/kwork_api_docs_probe_2026-07-08.json`
- `docs/kwork_api_docs_probe_auth_2026-07-08.json`
- `docs/kwork_api_docs_probe_post_auth_2026-07-08.json`
- `docs/kwork_demand_probe_snapshot_2026-07-08.json`
- `docs/kwork_supply_competitor_snapshot_2026-07-08.json`

### API docs / WSDL discovery facts

| Probe | Auth | Result |
|---|---|---|
| GET standard Swagger/OpenAPI paths on `api.kwork.ru` | none | nginx 401. |
| GET standard Swagger/OpenAPI paths on `api.kwork.ru` | mobile Authorization | HTTP 200 JSON `success=false`, error "Разрешен только HTTP метод POST", `error_code=403`. |
| POST standard Swagger/OpenAPI paths on `api.kwork.ru` | mobile Authorization | HTTP 200 JSON `success=false`, error "Метод API не найден", `error_code=404`. |
| GET/POST common SOAP/WSDL paths on `api.kwork.ru` | mobile Authorization | Same generic JSON errors; no WSDL/SOAP definition found. |

Checked REST doc paths: `/swagger.json`, `/swagger/v1/swagger.json`, `/api-docs`, `/openapi.json`, `/openapi.yaml`, `/v1/api-docs`, `/v2/api-docs`, `/.well-known/openapi.json`, `/api/swagger`, `/docs`, `/redoc`, `/swagger`, `/swagger-ui`, `/swagger-ui.html`.

Checked WSDL paths: `/service?wsdl`, `/service?WSDL`, `/service?singleWsdl`, `/service.asmx?wsdl`, `/Service.svc?wsdl`, `/Service.svc?singleWsdl`, `/ws/service.wsdl`, `/axis2/services/listServices`, `/cxf/services`.

### Demand count facts

| Endpoint | Auth | Params | Observed result |
|---|---|---|---|
| `POST /getWantsCount` | token+basic | `categories=all` | `570` |
| `POST /getWantsCount` | token+basic | `categories=41` | `49` |
| `POST /getWantsCount` | token+basic | `categories=59` | `2` |
| `POST /getWantsCount` | token+basic | `categories=286` | `15` |
| `POST /getWantsCount` | token+basic | `categories=113` | `5` |
| `POST /getWantsCount` | token+basic | `categories=112` | `19` |
| `POST /getWantsCount` | token+basic | `categories=46` | `51` |
| `POST /getWantsCount` | token+basic | `categories=306` | `1` |
| `POST /getWantsCount` | token+basic | `categories=25` | `10` |
| `POST /getWantsCount` | token+basic | `categories=44` | `2` |
| `POST /getWantsCount` | token+basic | `categories=56` | `5` |
| `POST /getWantsCount` | token+basic | `categories=72` | `8` |
| `POST /getWantsCount` | token+basic | `categories=all`, `query=telegram` | `39` |
| `POST /getWantsCount` | token+basic | `categories=all`, `query=ai` | `11` |
| `POST /getWantsCount` | token+basic | `categories=all`, `query=seo` | `23` |
| `POST /getWantsCount` | token+basic | `categories=all`, `query=python` | `5` |
| `POST /getWantsCount` | token+basic | `categories=all`, `query=marketplace` | `0` |
| `POST /getWantsCount` | token+basic | `categories=all`, `query=bot` | `1` |
| `POST /getWantsCount` | token+basic | `categories=all`, `query=logo` | `9` |
| `POST /getWantsCount` | token+basic | `categories=all`, `query=database` | `0` |

### Demand detail endpoint facts

| Endpoint | Auth | Params | Fact |
|---|---|---|---|
| `POST /projects` | token+basic | `categories`, `page`, optional `query` | Returned active wants list plus `paging` and `connects`; page size observed as 12 when enough results exist. |
| `POST /project` | token+basic | `id` | Returned dict for sampled want ids with `price`, `title`, `description`, `offers`, `time_left`, `parent_category_id`, `category_id`, `user_projects_count`, `user_hired_percent`, `user_active_projects_count`, `has_offer`, `possible_price_limit`, portfolio/kwork need flags. |
| `POST /want` | token+basic | `id` | Returned list length 1 for sampled want ids; complementary fields included `views` and `date_create`. |

Sampled ids where both `/project` and `/want` worked: `3213393`, `3213391`, `1785211`, `1800669`, `3213387`.

### Competitor/supply snapshot facts

| Source | Fact |
|---|---|
| `POST /kworks` seed pages | 8 seed pages sampled, 80 kwork cards collected. |
| Seed pages | 54 unique seller usernames observed. |
| Seed page price sample | Minimum visible price `500`, maximum visible price `50000`. |
| Top repeated sellers in sampled seed pages | `Vitaliy_DSGN=3`, `Mikhail_Aksenov=2`, `IG_designed=2`, `IrinaDana=2`, `MDevostator=2`, `Maxximus=2`, `Kristi86=2`, `sbor_kontaktov=2`, `imnotes=2`, `LittleSani=2`, `DESIGN-LOGO=2`, `DESIGN_LOGOTYPE=2`, `TopClick=2`, `ameganix=2`. |
| Enrichment endpoints used | `userByUsername`, `kworksCategoriesList`, `userKworks`, `getKworkDetails`, `getKworkDetailsExtra`, `getKworkReviews`, `getKworkAnswers`, `getKworkPortfolios`. |

Seed counts observed in this snapshot:

| Seed | categoryId | classifierId | `/kworks` count |
|---|---:|---:|---:|
| Marketplace design | 286 | 1433413 | 82945 |
| Links | 59 | | 45412 |
| Ready databases | 113 | 1117 | 34239 |
| Logos | 25 | 401928 | 33585 |
| Marketplaces | 112 | 1357 | 109366 |
| Telegram | 46 | 281 | 8789 |
| AI logos/infographics | 306 | 4200156 | 6672 |
| Programming broad | 41 | | 38093 |

## 2026-07-08 16:58 MSK PSR scanner facts

| Item | Fact |
|---|---|
| Backend method | `KworkMarketClient.get_market_intelligence_snapshot(...)` |
| API route | `POST /api/kwork/market/intelligence-snapshot` |
| Default file output | `docs/kwork_market_snapshots/kwork_market_intelligence_<UTC>.json` when `write_file=true` |
| Seed discovery | Uses `POST /catalogMainv2` recursive extraction; fallback is the known high-value seed list. |
| Supply source | Uses `POST /kworks` by `categoryId`/`classifierId` and page. |
| Demand source | Optional: uses `/getWantsCount` and `/projects` by seed category and query. |
| Competitor details | Optional: uses existing `getKworkDetails` enrichment path. |
| Live smoke file | `docs/kwork_market_snapshots/kwork_market_intelligence_20260708T135633Z.json` |
| Live smoke result | `seed_count=2`, `cards_seen=20`, `unique_sellers_seen=18`. |
| Live smoke first seed | `Дизайн для маркетплейсов`, `category_id=286`, `classifier_id=1433413`, `seed_kworks_count=82701`; `/kworks` count `82945`. |
| Test command | `PYTHONPATH=C:\psr pytest tests/unit/test_kwork_market.py tests/unit/test_kwork_routes.py -q` |
| Test result | `18 passed`. |

## Validated portfolio web loaders

| Endpoint | Method | Params | Fact |
|---|---|---|---|
| `/portfolio/load_form_kworks` | POST | `categoryId`, `attributesId` or `attributesId[]` | With `categoryId=41`, `attributesId=3587`: HTTP 200 JSON, `success=true`, `data.kworks=[]`, `data.hasOrders=false`. |
| `/portfolio/load_form_orders` | POST | `kworkId` | With `kworkId=29072946`: HTTP 200 JSON, `success=true`, `data.anotherVideos=[]`, `data.hasOrders=false`. |
| `/portfolio/load_form_orders_full` | POST | `kworkIds` | With `kworkIds=29072946`: HTTP 200 JSON, `success=true`, `data.orders=[]`. |
| `/portfolio/get_popup` | POST | `portfolioId` | With `portfolioId=14040541` from mobile `userByUsername` sample: HTTP 404 JSON `Страница не найдена`; needs other context/id or permissions. |

## 2026-07-08 17:07 MSK PSR desktop scanner facts

| Item | Fact |
|---|---|
| Desktop API helper | `api.getKworkMarketIntelligenceSnapshot(...)` calls `POST /api/kwork/market/intelligence-snapshot`. |
| Desktop UI | `desktop/src/pages/KworkMarket.tsx` has a top-toolbar `Market scan` button. |
| UI scanner defaults | `max_seeds=8`, `pages=1`, `include_demand=true`, `include_competitor_details=false`, `competitor_detail_limit=0`, `write_file=true`. |
| UI output shown | Generated time, seed count, sampled card count, unique seller count, runtime, top sellers, and written JSON path. |
| Build check | `npx vite build` in `desktop`: passed. |
| Type check note | `npx tsc --noEmit` still fails on pre-existing unrelated pages; no new Kwork Market scanner type errors were reported. |
| Backend regression check | `PYTHONPATH=C:\psr pytest tests/unit/test_kwork_market.py tests/unit/test_kwork_routes.py -q`: `18 passed`. |

## 2026-07-08 17:21 MSK seller intelligence scanner facts

| Item | Fact |
|---|---|
| Seller profile endpoint | `POST /userByUsername` with `username` returns public seller profile fields. |
| Seller category endpoint | `POST /kworksCategoriesList` with `user_id` returns seller category tabs/counts. |
| Seller inventory endpoint | `POST /userKworks` with `user_id`, `page` returns seller kwork cards; observed response shape can be a list directly under `response`. |
| PSR backend method | `KworkMarketClient.fetch_seller_intelligence(username)` combines `userByUsername`, `kworksCategoriesList`, and `userKworks`. |
| Scanner flags | `include_seller_details`, `seller_detail_limit`. |
| Snapshot output | Top-level `seller_intelligence`; aggregate field `seller_profiles_collected`. |
| Desktop scan default | `Market scan` now sends `include_seller_details=true`, `seller_detail_limit=8`. |
| Live smoke file | `docs/kwork_market_snapshots/kwork_market_intelligence_20260708T142059Z.json`. |
| Live smoke result | `seed_count=1`, `cards_seen=10`, `unique_sellers_seen=9`, `seller_profiles_collected=2`. |
| Live seller sample | `imnotes`: 5 category tabs, 6 sampled inventory kworks. `RuPartner`: 4 category tabs, 6 sampled inventory kworks. |
| Backend regression check | `PYTHONPATH=C:\psr pytest tests/unit/test_kwork_market.py tests/unit/test_kwork_routes.py -q`: `19 passed`. |
| Frontend build check | `npx vite build` in `desktop`: passed. |

## 2026-07-08 17:29 MSK market ranking facts

| Item | Fact |
|---|---|
| Snapshot field | `market_rankings` is now present at top level. |
| Aggregate field | `aggregate.top_opportunities` contains the highest ranked seed rows. |
| Ranking inputs | `/kworks` supply count, `/getWantsCount` demand count, sampled card prices, sampled seller counts. |
| Ranking metrics | `demand_per_1000_kworks`, `opportunity_score`, `sample_price_min`, `sample_price_max`, `sample_price_avg`, `seller_concentration`. |
| Ranking signals | `signals.demand_level`, `signals.supply_level`. |
| Live smoke file | `docs/kwork_market_snapshots/kwork_market_intelligence_20260708T142853Z.json`. |
| Live Telegram seed | `category_id=46`, `classifier_id=281`, supply `8780`, demand `51`, query `telegram=39`. |
| Live Telegram ranking | `demand_per_1000_kworks=5.809`, `opportunity_score=391.98`, `demand_level=high`, `supply_level=moderate`. |
| Live Telegram prices | min `500`, max `25000`, avg `3700.0`. |
| Tavily check | `tavily_map` with strict categories/projects/users/portfolio/kwork/API path filters returned no URLs. |
| Backend regression check | `PYTHONPATH=C:\psr pytest tests/unit/test_kwork_market.py tests/unit/test_kwork_routes.py -q`: `20 passed`. |
| Frontend build check | `npx vite build` in `desktop`: passed. |

## 2026-07-08 18:48 MSK widget and portfolio popup facts

Artifacts:

- `docs/kwork_widget_portfolio_probe_2026-07-08T154649Z.json`
- `docs/kwork_widget_portfolio_followup_2026-07-08T154755Z.json`

Probe context:

- Read-like only.
- Used current VPNTE local proxy on port `17990`.
- Session Hub returned 14 Kwork cookie names; cookie values were not stored.
- IP-like values were redacted from artifacts.

| Endpoint | Method | Params | Live fact |
|---|---|---|---|
| `/api/widget/get` | GET | `json`, `cat_type`, `count`, `popular_type`, `width`, `ref`, `user_style` | Returned HTTP 302. `Location` strips `ref` and keeps the widget params, e.g. `/api/widget/get?json=yes&cat_type=all&count=3&popular_type=1&width=250&user_style=`. Current shape is not useful for whole-market intelligence. |
| `/portfolio_large/1123924` | GET | path portfolio id | HTTP 200 HTML, about 3.9 KB, contains `portfolio`, `kwork`, and `control-overlay` markers. |
| `/portfolio_large/14040541` | GET | path portfolio id | HTTP 200 HTML, about 3.8 KB, contains `portfolio`, `kwork`, and `control-overlay` markers. |
| `/portfolio/get_popup` | POST | `portfolioId=1123924` or `portfolioId=14040541` | HTTP 404 JSON string `Страница не найдена`. CamelCase param is not the useful form. |
| `/portfolio/get_popup` | POST | `portfolio_id=1123924`, `portfolio_id=14040541`, `id=1123924`, `id=14040541` | HTTP 200 JSON `success=true`, keys `data`, `success`; `data.portfolio=null`; `data.additional` contains category taxonomy and empty binding arrays. |

Observed `/portfolio/get_popup` `data.additional` keys:

- `canDeletePortfolio`
- `kworks`
- `anotherVideos`
- `categories`
- `parentCategories`
- `order`
- `hasOrders`
- `workNum`

Practical ranking:

- Use `/portfolio_large/{portfolio_id}` for competitor portfolio details when a portfolio id is known.
- Treat `/portfolio/get_popup` as low priority for competitor enrichment until a context/parameter combination returns non-null `data.portfolio`.
- Do not spend whole-market scanner budget on `/api/widget/get`; it appears to be a public widget/referral helper, not a market dataset.

## 2026-07-08 18:56 MSK kwork sites and first portfolio facts

Artifacts:

- `docs/kwork_kworksites_firstportfolio_probe_2026-07-08T155357Z.json`
- `docs/kwork_firstportfolio_followup_2026-07-08T155534Z.json`

Probe context:

- Read-like JS-only endpoints.
- Used current VPNTE local proxy on port `17990`.
- Session Hub returned 14 Kwork cookies; cookie values were not stored.
- WSTG tracking: `WSTG-APIT-02`.

| Endpoint | Method | Params | Live fact |
|---|---|---|---|
| `/api/kwork/getkworksites` | GET | `kworkId`, `showHosts`, `offset` | HTTP 200 JSON `success=true`, keys `html`, `linksSites`, `orderedLinksSites`, `success`. For tested `kworkId=29072946` and `kworkId=1`, `linksSites=[]` and `html=""`. |
| `/kwork_first_portfolio/{kwork_id}` | POST | path `kwork_id`, body `isWebpAccepted=1` | With `29072946`, `1123924`, `14040541`: HTTP 404 JSON page-not-found because these were not valid kwork-with-portfolio ids for this endpoint. |
| `/kwork_first_portfolio/{kwork_id}` | POST | path `kwork_id`, body `isWebpAccepted=1` | With kwork ids known to have mobile `getKworkPortfolios` data (`36214125`, `42207254`, `14100522`, `43576198`, `21805649`): HTTP 200 HTML for 5/5. Responses were about 4.1-6.5 KB and contained `portfolio`, `kwork`, `control-overlay`, `content-item`, `portfolio-large` markers. |
| `/swagger.json`, `/api-docs`, `/openapi.json`, `/.well-known/openapi.json` on `kwork.ru` | GET | none | HTTP 404 HTML; no public OpenAPI/Swagger doc found on web host in this probe. |
| Same API-doc paths on `api.kwork.ru` | GET | none | HTTP 401 nginx HTML; no public OpenAPI/Swagger doc found on API host in this probe. |

Practical PSR implication:

- Use mobile `POST /getKworkPortfolios` when structured portfolio lists are needed by kwork id.
- Use web `POST /kwork_first_portfolio/{kwork_id}` as a fast direct HTML enrichment when the kwork has portfolios and only the first visible portfolio example is needed.
- Use web `GET/POST /portfolio_large/{portfolio_id}` when a specific portfolio id is already known.
- Keep `/api/kwork/getkworksites` as conditional low-priority enrichment for kworks that expose link/site rows; do not spend broad market scan budget on it unless card/details show site links exist.

## 2026-07-08 19:03 MSK portfolioList and userReviews resolved facts

Artifact:

- `docs/kwork_portfoliolist_userreviews_probe_2026-07-08T160244Z.json`

Source of parameter truth:

- Upstream `https://github.com/kesha1225/kwork` `docs/openapi.json` on branch `master`.
- Local installed `kwork-0.2.0` confirmed wrapper auth modes in `openapi_mixin.py`.

Probe context:

- Rotated VPNTE before live probe: `germanyvless1` to `kazakhstan1`.
- Used Kwork credentials from local `.env` only to acquire a mobile token in memory.
- Token/password values were not stored.
- WSTG tracking: `WSTG-APIT-02`.

| Endpoint | Auth | Required params | Optional params | Live fact |
|---|---|---|---|---|
| `POST /portfolioList` | basic | `user_id`, `category_id`, `page` | none observed | `user_id=3280&category_id=all&page=1` returned HTTP 200 JSON `success=true`, `response.portfolio` list length 12, `paging.total=30`, `pages=3`. |
| `POST /portfolioList` | basic | `user_id`, `category_id`, `page` | none observed | `user_id=9628615&category_id=all&page=1` returned HTTP 200 JSON `success=true`, `response.portfolio` list length 12, `paging.total=254`, `pages=22`. |
| `POST /portfolioList` | basic | `user_id`, `category_id`, `page` | none observed | `category_id=15` for `user_id=9628615` returned valid empty result with `paging.total=0`; category filter is real. |
| `POST /userReviews` | token+basic | `token`, `user_id`, `type` | `page` | `user_id=3280&type=all&page=1` returned HTTP 200 JSON `success=true`, 30 reviews, `paging.total=1761`, `pages=59`. |
| `POST /userReviews` | token+basic | `token`, `user_id`, `type` | `page` | `type=positive` returned 30 reviews, `paging.total=1743`; `type=negative` returned 18 reviews, `paging.total=18`, `pages=1`. |

Allowed `userReviews.type` values from upstream OpenAPI:

- `all`
- `positive`
- `negative`

Response fields:

- `portfolioList`: top-level keys include `success`, `response`; `response` has `portfolio`, `paging`.
- `userReviews`: top-level keys include `success`, `response`, `paging`; review item keys include `id`, `time_added`, `text`, `auto_mode`, `good`, `bad`, `kwork`, `writer`, and sometimes `answer`.

Practical PSR implication:

- `portfolioList` is no longer unresolved. Use it as the structured seller portfolio inventory endpoint by `user_id` and `category_id=all` or a category id.
- `userReviews` is no longer unresolved, but it requires a mobile token. Use it for seller reputation totals, positive/negative split, review texts, buyer/writer metadata, and linked kwork snippets.
- For public/no-token fallback, keep using `userByUsername.reviews` and `getKworkReviews`, but authenticated market scans should prefer `userReviews` when seller-level review history is needed.

## 2026-07-08 17:45 MSK snapshot history reader facts

| Item | Fact |
|---|---|
| Backend method | `KworkMarketClient.get_market_intelligence_history(limit=50, output_dir=None)`. |
| API route | `GET /api/kwork/market/intelligence-history?limit=<n>`. |
| Source files | Reads `docs/kwork_market_snapshots/latest.json` and `docs/kwork_market_snapshots/index.jsonl`. |
| Error behavior | Malformed JSONL lines are skipped. |
| Desktop API helper | `api.getKworkMarketIntelligenceHistory(limit)`. |
| Desktop UI | `KworkMarket.tsx` toolbar has a `History` button and a compact history panel. |
| Live local reader | `exists=true`, `entry_count=1`, `latest_generated_at=2026-07-08T14:36:44Z`. |
| Backend regression check | `PYTHONPATH=C:\psr pytest tests/unit/test_kwork_market.py tests/unit/test_kwork_routes.py -q`: `22 passed`. |
| Frontend build check | `npx vite build` in `desktop`: passed. |

## 2026-07-08 17:49 MSK web JS endpoint extraction facts

| Item | Fact |
|---|---|
| JS extraction artifact | `docs/kwork_js_endpoint_snapshot_2026-07-08T144640Z.json`. |
| Live status artifact | `docs/kwork_js_endpoint_live_status_2026-07-08T144816Z.json`. |
| Sources inspected | `51`. |
| JS scripts fetched | `46`. |
| Endpoint-like paths extracted | `348`. |
| Interesting paths after filtering | `333`. |
| New filtered web/API-like paths | `37`. |
| New order endpoint candidate | `/api/order/getorderscount`. |
| New seller/user endpoint candidate | `/api/user/getworkbaysellers`. |
| New billing endpoint candidate | `/api/bill/address_suggestions`. |
| New offer endpoint candidate | `/api/offer/addview`. |
| New upload endpoint candidate | `/api/file/upload`. |
| New cart endpoints | `/api/cart/add`, `/api/cart/delete`, `/api/cart/order`. |
| New portfolio API candidates | `/api/portfolio/hidelimitnotice`, `/api/portfolio/youtubelinkvalidate?url=`, `/portfolio_large/`, `/portfolio/log_portfolio_view`, `/portfolio/add`. |
| New portfolio upload/status candidates | `/portfolio/upload_image`, `/portfolio/upload_video`, `/portfolio/upload_audio`, `/portfolio/upload_pdf`, `/portfolio/alternate_upload_cover`, `/portfolio/alternate_upload_image`, `/portfolio/alternate_upload_video`, `/portfolio/check_audio_ready`, `/portfolio/check_video_thumbnail_ready`, `/portfolio/check_gif_thumbnail_ready`. |
| New catalog filter candidate | `/catalog_kworks_filters/`. |
| Live check `/api/user/getworkbaysellers` | GET returned HTTP 200 JSON with auth-required failure. |
| Live check `/api/bill/address_suggestions` | GET returned HTTP 200 JSON with auth error payload. |
| Live check `/portfolio_large/` | GET returned HTTP 302 to `http://kwork.ru/portfolio_large`. |
| Live check `/catalog_kworks_filters/` | GET returned HTTP 302 to `http://kwork.ru/catalog_kworks_filters`. |
| Tavily crawl fact | `tavily_crawl` without research mode returned public category pages only and no API-like URLs. |

## 2026-07-08 17:53 MSK priority JS endpoint context facts

| Item | Fact |
|---|---|
| Context artifact | `docs/kwork_js_priority_endpoint_context_2026-07-08T145218Z.json`. |
| Safe probe artifact | `docs/kwork_js_priority_endpoint_probe_2026-07-08T145245Z.json`. |
| `/api/order/getorderscount` context | JS names: `getActiveOrdersCount`, `ajaxGetActiveOrdersCount`, `updateOrdersCount`; GET/POST hints. |
| `/api/order/getorderscount` probe | POST returned HTTP 200 JSON body `false`; GET returned maintenance HTML. |
| `/api/user/getworkbaysellers` context | JS names include `getWorkbaySellers`, `sellers`; payment/company modal context; GET/POST hints. |
| `/api/user/getworkbaysellers` probe | GET returned HTTP 200 JSON auth-required payload; POST without proper context returned maintenance HTML. |
| `/api/offer/addview` context | JS names include `wantAddViews`, `wantIds`; POST hint. |
| `/api/file/upload` context | JS names include `uploadFile`, `updateFileIndex`, `cancelToken`, `onUploadProgress`; POST hint. |
| `/api/cart/*` context | Cart add/delete/order flow found; mutating endpoints, not live-probed beyond static extraction. |
| `/api/portfolio/youtubelinkvalidate?url=` context | Params include `url`, `kwork_id`, `portfolio_id`; POST hint. |
| `/api/portfolio/youtubelinkvalidate?url=` probe | POST returned HTTP 401 JSON access denied. |
| `/portfolio_large/` context | JS names include `getPortfolio`, `getFirstPortfolio`, `setAllIds`, `standAlone`; portfolio popup/detail loader candidate. |
| `/portfolio/log_portfolio_view` context | Params include `portfolio_video_id`, `view_type`; POST analytics endpoint. |
| `/catalog_kworks_filters/` context | JS names include `updateCatalogParams`, `aliasWithFilterPostParams`, `excludeIds`, `onePage`, `showMoreCount`, `getAliasWithFilterParamsPostFormData`. |
| `/catalog_kworks_filters/` probe | POST without canonical URL/context returned HTTP 302 canonical redirect. |

## 2026-07-08 18:00 MSK read-like web endpoint facts

Artifacts:

- `docs/kwork_readlike_endpoint_probe_2026-07-08T145632Z.json`
- `docs/kwork_readlike_endpoint_summary_2026-07-08T150029Z.json`

| Endpoint | Method | Params | Fact |
|---|---|---|---|
| `/catalog_kworks_filters/programming` | POST | `page=1`, `pageSize=10` | HTTP 200 JSON, `success=true`, about 319 KB, contains `data.stateData.viewData.filters` and `data.stateData.viewData.kworks`. |
| `/catalog_kworks_filters/design` | POST | `page=1`, `pageSize=10` | HTTP 200 JSON, `success=true`, about 407 KB, contains the same `stateData.viewData` shape. |
| `/catalog_kworks_filters/telegram-boty` | POST | `page=1`, `pageSize=10` | HTTP 200 JSON, `success=false`, `data=null`; alias must be canonical. |
| `/portfolio_large/1123924` | GET/POST | path portfolio id | HTTP 200 HTML, about 4 KB, contains portfolio/kwork markers and `control-overlay` navigation. |
| `/portfolio_large/14040541` | GET/POST | path portfolio id | HTTP 200 HTML, about 4 KB, contains portfolio/kwork markers and `control-overlay` navigation. |
| `/portfolio_large/29072946` | POST | path kwork-like id | HTTP 404 JSON page-not-found; confirms this path wants a portfolio id, not a kwork id. |

Observed `catalog_kworks_filters` state keys:

- `stateData`: `viewData`, `isWidePage`, `isRedesign`, `isRedesignCard`, `isRedesignFilter`, `isShowSticker`, `isNeedYescrowDigitalSign`.
- `viewData`: `filters`, `kworks`.
- Useful filter keys: `kworksCount`, `attributesIds`, `attributesTree`, `selectedAttributes`, `filterAttributes`, `selectedAttributesIds`, `multipleAttributes`, `paymentTypeFilter`, `attributeReviews`, `packageFilters`, `activeCategoryId`, `priceLimits`, `priceFilterBounds`, `queryParamsJson`, `volumeTypes`, `activeCat`, `sellerLvl`, `sMinReview`, `sOrdersQueue`, `sMinUserSales`, `sMinKworkSales`, `sonline`, `filterPrice`.

Practical use:

- Use `/catalog_kworks_filters/{canonical_alias}` when PSR needs web-catalog cards plus the exact web filter state in one same-origin JSON response.
- Use `/portfolio_large/{portfolio_id}` to open competitor portfolio details from known portfolio ids without loading a full public profile page.

## 2026-07-08 18:06 MSK catalog alias facts

Artifacts:

- `docs/kwork_catalog_alias_probe_2026-07-08T150507Z.json`
- `docs/kwork_catalog_alias_deep_probe_2026-07-08T150630Z.json`

| Item | Fact |
|---|---|
| Tavily map/crawl | Without research mode, Tavily still returned only the 7 public root category URLs. |
| HTML alias extraction | Direct extraction from the 7 root category pages found 61 `/categories/{alias}` candidates. |
| Fast POST sweep | 18 aliases returned HTTP 200 JSON `success=true`; 43 later checks returned HTTP 403 HTML. |
| 403 interpretation | A follow-up check immediately returned 403 even for root category pages, so treat the 403 set as QRATOR/rate/session-protection affected, not as invalid aliases. |
| Confirmed successful aliases | `design`, `programming`, `writing-translations`, `seo`, `promotion`, `audio-video`, `business`, `business-copywriting`, `creative-writing`, `interior-exterior-design`, `personal-assistant`, `presentations-infographics`, `usability-testing`, `analytics`, `animation`, `audio`, `audit`, `bulletin-boards`. |
| Useful unvalidated aliases from extraction | `links`, `script-programming`, `website-development`, `frontend`, `mobile-apps`, `smm`, `logo`, `traffic`, `information-bases`, `imagegeneration`, `textgeneration`, `videogeneration`, `server-administration`, `software`, `marketing`. |
| Response shape correction | In summary probes, `data.stateData.viewData.kworks` was a `dict`, not a simple list; parser must inspect keys instead of assuming mobile `/kworks` shape. |
| Category id mapping examples | `programming -> 11`, `design -> 15`, `writing-translations -> 5`, `seo -> 17`, `promotion -> 45`, `audio-video -> 7`, `business -> 83`, `bulletin-boards -> 112`, `audit -> 44`, `analytics -> 56`, `presentations-infographics -> 270`. |
| Autopentest priority | `prioritize_endpoints` ranked `POST /catalog_kworks_filters/{alias}` score `24.5`, highest among the current market-intelligence endpoints. |

Practical PSR implication:

- Add a throttled same-origin web-catalog collector for `/catalog_kworks_filters/{alias}`.
- Use it to cross-check browser-real category/filter state against mobile `/kworks`.
- Store protection status (`ok`, `403`, `rate_limited`) so scanner history does not confuse QRATOR blocks with empty market data.

PSR implementation fact:

| Item | Fact |
|---|---|
| Backend helper | `KworkMarketClient.get_web_catalog_filters(alias, page=1, page_size=10, include_raw=False, cookies=None)` now calls `POST /catalog_kworks_filters/{alias}` and returns compact state. |
| Parser | `summarize_web_catalog_state(...)` handles `viewData.kworks` as either list or dict and records filter/kwork schema summaries. |
| Protection status | Result includes `protection_status`: `ok`, `blocked`, `http_error`, `parse_error`, or `empty_or_invalid_alias`. |
| API route | `GET /api/kwork/market/web-catalog/{alias}` exposes a one-alias manual probe. |
| Batch collector | `KworkMarketClient.get_web_catalog_alias_snapshot(...)` collects selected aliases with `delay_seconds` and stores status counts. |
| API route | `POST /api/kwork/market/web-catalog-snapshot` exposes the throttled batch collector. |
| Desktop helpers | `api.getKworkWebCatalog(alias, options)` and `api.getKworkWebCatalogSnapshot(payload)` are available in `desktop/src/lib/api.ts`. |
| Tests | Backend Kwork market/routes tests: `26 passed`. Frontend `npx vite build`: passed. |

## 2026-07-08 18:19 MSK Session Hub web catalog smoke

Artifact: `docs/kwork_web_catalog_snapshots/kwork_web_catalog_alias_snapshot_20260708T151918Z.json`.

| Item | Fact |
|---|---|
| Session Hub | Returned 17 cookies; cookie values were not stored. |
| Probe | Throttled snapshot for `programming` and `design`, `delay_seconds=2`, `page=1`, `pageSize=10`. |
| Result | Both aliases returned HTTP 403 HTML, 1726 bytes, `protection_status=blocked`. |
| Block body | Page says access was blocked because of high load from this side, likely automated scripts. |
| WAF fingerprint | `autopentest_ai.identify_waf` did not match a known WAF vendor from the QRATOR/header/body sample. |
| Interpretation | Current response is protection/rate/fingerprint block, not empty market data and not proof that aliases are invalid. |

## 2026-07-08 18:24 MSK mobile misc endpoint facts

Artifact: `docs/kwork_mobile_misc_probe_2026-07-08T152410Z.json`.

The artifact has redacted IP-like values from remote error payloads.

| Endpoint | Params | Live fact |
|---|---|---|
| `POST /exchangeInfo` | none | Currently blocked at mobile/API auth layer with HTTP 403. Candidate for market stats only if token auth recovers. |
| `POST /getCurrentVersions` | none | Currently HTTP 403. Useful only as API-version signal, not market intelligence. |
| `POST /getCaptchaStatus` | none | Currently HTTP 403 through sign-in path. Account health signal, not market intelligence. |
| `POST /getBadgesInfo` | body `{}` | Currently HTTP 403 through sign-in path. Account notification signal, not market intelligence. |
| `POST /kworksStatusList` | none | Parameter error in current cookie-only/auth-blocked state. Own-kworks status, not whole-market data. |
| `POST /getUserInfo` | `id=3280` | Still returned unknown API error with simple `id`. Use `POST /user` and `POST /userByUsername` instead. |
| `POST /ordersBetween` | `user_id=3280` | Parameter error in current state. Account-specific relationship history, not whole-market popularity data. |

Practical ranking:

- Keep whole-market scanner focused on validated endpoints: `/kworks`, `/projects`, `/getWantsCount`, `/catalogMainv2`, `/catalogFilters`, `/categoryAttributes`, `userByUsername`, `userKworks`, `getKworkDetails`, `getKworkDetailsExtra`, `getKworkReviews`.
- Do not spend scanner budget on `ordersBetween`, `getBadgesInfo`, `getCaptchaStatus`, or `kworksStatusList` for market popularity scoring.

## 2026-07-08 17:37 MSK snapshot history facts

| Item | Fact |
|---|---|
| Full snapshot file | Still written as `docs/kwork_market_snapshots/kwork_market_intelligence_<UTC>.json`. |
| Latest summary file | `docs/kwork_market_snapshots/latest.json`. |
| Append-only history file | `docs/kwork_market_snapshots/index.jsonl`. |
| Summary contents | `generated_at`, `file_path`, compact config, aggregate counts, top opportunities, top sellers, query demand counts, timings. |
| Desktop UI | `Market scan` now shows `snapshot`, `latest`, and `history` paths when present. |
| Live smoke file | `docs/kwork_market_snapshots/kwork_market_intelligence_20260708T143644Z.json`. |
| Live smoke latest | `docs/kwork_market_snapshots/latest.json`. |
| Live smoke index | `docs/kwork_market_snapshots/index.jsonl`. |
| Live smoke result | Telegram seed: `cards_seen=10`, `unique_sellers_seen=9`; latest/index contain compact summary. |
| Backend regression check | `PYTHONPATH=C:\psr pytest tests/unit/test_kwork_market.py tests/unit/test_kwork_routes.py -q`: `20 passed`. |
| Frontend build check | `npx vite build` in `desktop`: passed. |

## 2026-07-08 19:13 MSK public mobile-basic market signal facts

Artifacts:

- `docs/kwork_public_market_signal_probe_2026-07-08T160926Z.json`
- `docs/kwork_public_market_signal_probe_basic_2026-07-08T161101Z.json`
- `docs/kwork_public_market_signal_probe_mobilebasic_2026-07-08T161227Z.json`
- `docs/kwork_search_signal_ru_probe_2026-07-08T161307Z.json`

Source context:

- Tavily found the upstream Python `kwork` wrapper and JS `kwork-api` wrapper.
- Installed `kwork` package defines the mobile API header as `Authorization: Basic bW9iaWxlX2FwaTpxRnZmUmw3dw==`.
- Plain unauthenticated `requests` calls and account login/password HTTP basic both returned nginx HTTP 401 for the tested `api.kwork.ru` endpoints.
- Replaying the same calls with the mobile basic header returned HTTP 200.

Live facts:

| Endpoint | Params | Live fact |
|---|---|---|
| `POST /search` | `query=telegram bot`, `limit=5`, `page=1` | HTTP 200 JSON `success=true`; response contains `kworks_count=5`, `kworks`, `classifiers`, `searchCategories`, `paging`. Useful keyword supply probe. |
| `POST /search` | `query=telegram bot`, `categoryId=15`, `limit=5`, `page=1` | HTTP 200 JSON `success=true`; response contains `kworks_count=3`, `kworks`, `classifiers`, `paging`. Category-scoped keyword probe works. |
| `POST /searchKworksCatalogQuery` | `query=telegram` | HTTP 200 JSON `success=true`; response list length 9 with `suggestion` and `excerpt`. Useful as autocomplete/keyword-discovery source, but language behavior needs care. |
| `POST /searchKworksCatalogQuery` | `query=телеграм` | HTTP 200 JSON `success=true`, but response list length 0 in this mobile-basic probe. |
| `POST /search` | `query=телеграм`, `limit=5`, `page=1` | HTTP 200 JSON `success=true`, but `kworks_count=0` in this mobile-basic probe. |
| `POST /search` | `query=логотип`, `categoryId=15`, `limit=5`, `page=1` | HTTP 200 JSON `success=true`, but `kworks_count=0` in this mobile-basic probe. |
| `POST /getKworkLinksTable` | `id=36214125`, `page=1` | HTTP 200 JSON `success=true`, list response empty, `paging.total=0`. Endpoint is live but conditional. |
| `POST /getKworkLinksTablev2` | `id=36214125`, `page=1` | HTTP 200 JSON `success=true`, `response.link_headers=[]`, `links_content=[]`, `paging.total=0`. Endpoint is live but conditional. |
| `POST /category` | `category_id=15` | HTTP 200 JSON `success=true`; response keys include `id`, `name`, `mobile_description`, `base_volume`, `volume_type`, `attributes`, `positive_reviews_count`. |
| `POST /positiveReviewsCount` | `category_id=15`; also `category_id=15`, `attribute_id=208` | HTTP 200 JSON `success=true`, but `response=false` for tested inputs. Candidate remains conditional, not proven useful yet. |

Practical PSR implication:

- Use `/search` as a mobile keyword supply probe, especially for English/transliterated queries, but do not replace category/classifier `/kworks` for Russian market scans until locale/query behavior is understood.
- Use `/searchKworksCatalogQuery` as an autocomplete seed source; store zero-result language probes separately from endpoint failure.
- Keep `/getKworkLinksTable` and `/getKworkLinksTablev2` as optional competitor enrichment for link/site-heavy kworks; empty response for one kwork is not endpoint failure.
- `category` is a cheap category metadata endpoint and can fill category labels/descriptions.
- `positiveReviewsCount` needs a follow-up sweep over real attribute IDs/categories before using it as a popularity metric.

## 2026-07-08 19:24 MSK authenticated demand/project API facts

Artifact: `docs/kwork_demand_api_probe_direct_2026-07-08T162342Z.json`.

Probe context:

- VPNTE control API rotated earlier in this segment; during the direct demand probe status reported profile `usa1`, country `United States`, local proxy URL `http://127.0.0.1:17990`.
- The probe used the installed `kwork.Kwork` mobile client directly with explicit proxy, timeout, and retry limits.
- Kwork credentials from local environment were used only in memory; token acquisition succeeded and token/password were not stored.
- Earlier full PSR-service probe timed out before writing an artifact; the direct mobile-client probe is the valid evidence.

Live facts:

| Endpoint | Params | Live fact |
|---|---|---|
| `POST /getWantsCount` | `categories=all` | HTTP/API success through mobile client; `response.count=569`. Cheap whole-market demand total. |
| `POST /projects` | `categories=all&page=1` | 12 projects, `paging.total=569`, `limit=12`, `pages=48`; `connects` block includes account connect balance. |
| `POST /projects` | `categories=11&page=1` | 12 projects, `paging.total=164`, `pages=14`. Category filter is real and fast. |
| `POST /projects` | `categories=all&page=1&query=telegram` | 12 projects, `paging.total=40`, `pages=4`. Query filter is real and useful for demand keyword scanning. |
| `POST /projects` | `categories=all&page=1&kworks_filter_to=5` | 12 projects, `paging.total=239`, `pages=20`. Low-offer filter is real and useful for opportunity scoring. |
| `POST /project` | `id=3213527` | Returns card-like project detail with keys including `id`, `status`, `user_id`, `username`, `price`, `title`, `description`, `offers`, `time_left`, `parent_category_id`, `category_id`, `user_hired_percent`, `allow_higher_price`, `possible_price_limit`, `has_offer`, `user_need_portfolio`. |
| `POST /want` | `id=3213527` | Returns richer want detail with keys including `id`, `title`, `description`, `status`, `want_status_id`, `date_create`, `date_active`, `date_expire`, `price_limit`, `views`, `orders`, `offers`, `views_history`, `allow_higher_price`, `possible_price_limit`. |

Practical PSR implication:

- Use `getWantsCount` as the cheapest demand-count primitive for market dashboards and filter sweeps.
- Use `projects` for paginated buyer demand samples, category totals, query totals, price filters, hiring filters, and low-offer opportunity detection.
- Use `want` rather than `project` when PSR needs richer buyer-demand details, especially `views`, `orders`, `views_history`, and active/expiry dates.
- Keep `project` as a compatible card-detail endpoint because it mirrors `/projects` card shape and includes buyer profile metrics.
- Demand response fields that should feed ranking: `price`, `offers`, `time_left`, `category_id`, `parent_category_id`, `user_projects_count`, `user_hired_percent`, `user_active_projects_count`, `allow_higher_price`, `possible_price_limit`, `user_need_kwork`, `user_need_portfolio`, plus `/want` `views`, `orders`, and `views_history`.

## 2026-07-08 19:31 MSK metadata and aggregate market API facts

Artifacts:

- `docs/kwork_metadata_aggregate_probe_2026-07-08T162920Z.json`
- `docs/kwork_catalog_filter_followup_2026-07-08T163032Z.json`

Probe context:

- VPNTE proxy rotated from `usa1` to `usa2`; local data proxy remained `http://127.0.0.1:17990`.
- API requests used the installed-library mobile basic header. No user token was needed for this metadata sweep.

Live facts:

| Endpoint | Params | Live fact |
|---|---|---|
| `POST /categories` | none | HTTP 200 JSON `success=true`; response list length 7 root categories. Items have `id`, `name`, `description`, `subcategories`. Root examples: `15 Дизайн`, `11 Разработка и IT`, `5 Тексты и переводы`. |
| `POST /category` | `category_id=11` / `15` | HTTP 200; compact category metadata with `id`, `name`, `mobile_description`, `base_volume`, `volume_type`, `attributes`, `positive_reviews_count`. For 11 and 15, `positive_reviews_count=false`. |
| `POST /catalogMainv2` | none | HTTP 200; large catalog home payload with `popular_categories_block`, `catalog_service_block`, `other_services_block`. Popular category cards include `category_id`, `classifier_id`, `name`, `cover_url`, `cover_redesign_url`, `mobile_cover_url`, `kworks_count`, `order`. |
| `POST /catalogFilters` | `categoryId=11` | HTTP 200; `response.filters`, `groups`, `kworks_count=103023`. |
| `POST /catalogFilters` | `categoryId=15` | HTTP 200; `kworks_count=259863`. |
| `POST /catalogFilters` | `categoryId=25` | HTTP 200; `kworks_count=46170`, 6 filters including price range, seller level, seller activity. |
| `POST /catalogFilters` | `categoryId=41` | HTTP 200; `kworks_count=38135`, 6 filters including price range, seller level, seller activity. |
| `POST /catalogFilters` | `category_id=11/25/41` | HTTP 200 but often returned generic `kworks_count=784454` and different filter count; do not use snake_case `category_id` here for market counts. |
| `POST /categoryAttributes` | root `category_id=11` / `15` | HTTP 200 `success=true`, but empty response list. Root categories do not expose useful classification trees here. |
| `POST /categoryAttributes` | child `category_id=25` / `41` | HTTP 200 list length 1. Returns classification tree with root attribute `Тип`, fields like `is_classification`, `required`, `percent_usage`, `alias`, `children`, `meta_title`, `kworks_count`. |
| `GET /api/freeprice/categorygetprices` | `categoryId=11/15&lang=ru` | HTTP 200 with `success=true`, but `prices` empty/null in this probe. |
| `GET /api/freeprice/attributegetprices` | `categoryId=11&attributeId=208&lang=ru` | HTTP 200 with `success=true`, but `prices` empty/null in this probe. |
| `POST /positiveReviewsCount` | `category_id=11/15`, `attribute_id=208` | HTTP 200 `success=true`, `response=false` for tested inputs. Still not proven useful. |

Practical PSR implication:

- Use `catalogMainv2` as a ready-made market seed source: it already contains curated popular niches with `category_id`, `classifier_id`, `kworks_count`, images, and ordering.
- Use `catalogFilters` with camelCase `categoryId` for aggregate supply counts and available filter dimensions. Treat snake_case `category_id` on this endpoint as misleading for counts.
- Use `categoryAttributes` on leaf/child categories, not roots, to get classification trees and deep classifier candidates.
- Use `/categories` for root/subcategory taxonomy, then `catalogMainv2` and `categoryAttributes(child)` for scan seeds.
- Freeprice endpoints and `positiveReviewsCount` remained low-value in this sweep because they returned success with empty/null payloads for tested inputs.

## 2026-07-08 19:37 MSK seller and competitor detail API facts

Artifact: `docs/kwork_seller_competitor_probe_2026-07-08T163639Z.json`.

Probe context:

- VPNTE proxy rotated from `usa2` to profile `turkeyvless1`; status country during probe was `Germany`, local proxy URL stayed `http://127.0.0.1:17990`.
- Probe selected a real competitor from `POST /kworks categoryId=41`: `kwork_id=255535`, worker `teleprog`, `user_id=198065`.
- Mobile token was acquired in memory for token-required endpoints; token/password were not stored.

Live facts:

| Endpoint | Params | Live fact |
|---|---|---|
| `POST /userByUsername` | `username=teleprog` | HTTP/API success. Response profile keys include `id`, `username`, `fullname`, `description`, `rating`, `rating_count`, `level_description`, `good_reviews`, `bad_reviews`, `reviews_count`, `online`, `live_date`, `custom_request_min_budget`, `is_allow_custom_request`, order completion percentages, achievements, `completed_orders_count`, `specialization`, `profession`, `kworks_count`, embedded `kworks`, `portfolio_list`, `reviews`, `skills`, `is_verified_worker`. |
| `POST /user` | `id=198065` | Same broad profile shape as `userByUsername`; use when numeric id is already known. |
| `POST /kworksCategoriesList` | `user_id=198065` | HTTP/API success, response list length 2. Items have `kworks_count`, `id`, `name`, `kworks`. Categories: desktop programming and scripts/bots/mini apps. |
| `POST /userKworks` | token, `user_id=198065`, `page=1` | HTTP/API success, response list length 2, paging total 2. Items match catalog card shape: `id`, `category_id`, `category_name`, `title`, `share_url`, `image_url`, `price`, `worker`, `activity`, badges. |
| `POST /portfolioList` | `user_id=198065`, `category_id=all`, `page=1` | HTTP/API success, `response.portfolio` count 12. Portfolio items include `id`, `title`, `order_id`, `category_id`, `category_name`, `type`, `photo`, `views`, `views_dirty`, `comments_count`, `images`, `videos`, `audios`, `pdf`. |
| `POST /userReviews` | token, `user_id=198065`, `type=all`, `page=1` | HTTP/API success, 30 rows, paging total 378, 13 pages. Item keys include `id`, `review_display_id`, `time_added`, `text`, `auto_mode`, `good`, `bad`, `kwork`, `writer`. |
| `POST /userReviews` | token, `user_id=198065`, `type=negative`, `page=1` | HTTP/API success, 2 rows, paging total 2. Negative rows include `answer`. |
| `POST /userSearch` | `query=teleprog`, `page=1` | HTTP/API success, response list length 2, paging total 2. Items have `id`, `username`, `fullname`, `profilepicture`, `rating`, `reviews_count`, `rating_count`, `is_online`. |
| `POST /getKworkDetailsExtra` | `id=255535` | HTTP/API success. Response keys include `recommended_kworks`, `similar_kworks`, `other_kworks`, `goodReviews`, `badReviews`, `frequently_asked_questions_count`, `last_reviews`, `reviews_count`, `kwork_ratings`. For the tested kwork, `reviews_count=121`. |
| `POST /getKworkAnswers` | `id=255535` | HTTP/API success but `response=None` for this kwork. Endpoint is live but only useful when FAQ exists. |
| `POST /getKworkPortfolios` | `id=255535`, `page=1` | HTTP/API success, empty list and paging total 0 for this kwork. |
| `POST /getKworkReviews` | `kwork_id=255535`, `type=all`, `page=1` | HTTP/API success, 30 rows, paging total 121, 5 pages. |

Practical PSR implication:

- Current seller enrichment should be upgraded: after `userByUsername/userKworks/kworksCategoriesList`, add `portfolioList` and `userReviews` when authenticated, because they expose seller portfolio views/media and full review history.
- Prefer `userByUsername` when starting from catalog worker username; prefer `user` when starting from `user_id`.
- Use `getKworkDetailsExtra` as a competitor expansion endpoint: `recommended_kworks`, `similar_kworks`, and `other_kworks` reveal adjacent competitors without extra catalog search.
- Use seller-level `userReviews` for reputation and negative-review mining; use kwork-level `getKworkReviews` for one-card social proof.
- Use `portfolioList` for competitor portfolio/media analysis; it contains views and media arrays even when individual kwork portfolios are empty.
- Keep `getKworkAnswers` and `getKworkPortfolios` conditional: empty response for one kwork is not endpoint failure.

Implementation note 2026-07-08 21:20 MSK:

- `src/platforms/kwork_market.py` now wires `portfolioList(user_id, category_id=all, page=1)` into `fetch_seller_intelligence()` as a capped `portfolio` summary.
- `src/platforms/kwork_market.py` now wires `userReviews(user_id, type=all/negative, page=1)` into `fetch_seller_intelligence()` as capped review summaries with totals and negative-answer flags.
- `src/platforms/kwork_market.py` now wires `getKworkDetailsExtra(id)` into `fetch_competitor_detail()` as `extra.review_summary`, `extra.faq_count`, and capped adjacent `recommended/similar/other` kworks.
- `/projects` demand samples are now normalized before entering market snapshots: `offers`, `price`, `possible_price_limit`, `time_left`, `user_hired_percent`, `user_need_kwork`, `user_need_portfolio`, `allow_higher_price`, dates, views/orders. `build_market_rankings()` now adds low-offer, budget, portfolio-required, kwork-required, and higher-price-allowed aggregates.
- Seed discovery now merges `catalogMainv2` with taxonomy from `catalogRubrics()` and `catalogCategories(rubricId)`, deduped by `(category_id, classifier_id)` and sorted by `kworks_count`.
- Optional `/want` detail enrichment is wired through `include_want_details` and `want_detail_limit`; it stores compact views, orders, views_history count, dates, and short description for capped top demand samples.
- Optional category price rules are wired through `include_price_rules`; market supply/rankings now preserve `price_rule_min`, `price_rule_max`, and `price_rule_typical` from freeprice responses.
- Optional account context is wired through `include_account_context`; it summarizes `actor`, `getActorInfo`, `kworksStatusList`, and `offers` separately from public market rankings.
- Route and desktop API contracts now expose `include_want_details`, `want_detail_limit`, `include_price_rules`, and `include_account_context`; Kwork Market UI exposes scan toggles for prices/wants/account.
- `KworkService.get_token_api()` now separates token-required mobile endpoints from cookie-only Session Hub clients and can fall back to direct email/password auth when credentials are configured.
- Token-mode demand live smoke succeeded after setting process-local credentials and rotating VPNTE: `getWantsCount(categories=41)` returned `46`, `projects(categories=41,page=1)` returned 12 rows, market snapshot demand status was `ok`, first demand sample included `/want` detail, and price rules status was `ok`.
- Full capped live smoke succeeded after route/frontend/token fallback work: supply (`/kworks`), competitor details (`getKworkDetails`, `getKworkDetailsExtra`), demand (`getWantsCount`, `projects`, `want`), price rules (`categorygetprices`), seller enrichment (`userByUsername`, `kworksCategoriesList`, `userKworks`, `portfolioList`, `userReviews`), and account context (`actor`, `getActorInfo`, `kworksStatusList`, `offers`) all returned compact usable data.
- Tavily external pass found no official public documentation for the hidden endpoint map. The useful external corroboration remains the public `/projects` page and third-party `pykwork` wrapper; PSR's live/JS/API evidence is the authoritative source for the hidden endpoints.
- Live smoke saved to `docs/kwork_market_integration_live_smoke_2026-07-08.json`: `catalogMainv2 + catalogRubrics + catalogCategories + kworks` returned status `ok`, 5 seeds, and a 2-seed supply snapshot with 20 cards.
- Live smoke saved to `docs/kwork_market_demand_price_live_smoke_env_2026-07-08.json`: category price rules worked, but demand stayed empty because current service auth was Session Hub `cookie-only`; token-required `/projects/getWantsCount` still needs token-mode auth for live verification.
- Live smoke saved to `docs/kwork_market_token_demand_live_smoke_2026-07-08.json`: token-mode demand path worked, including `/getWantsCount`, `/projects`, `/want`, `/kworks`, and category freeprice rules.
- Live smoke saved to `docs/kwork_market_full_api_live_smoke_2026-07-08.json`: full capped scan returned `status=ok`, 10 supply cards, competitor detail+extra ok, demand ok with `/want`, price rules ok, 2 seller profiles with portfolio/reviews, and account context ok.
- Latest local checks: `python -m pytest tests/unit/test_kwork_service.py tests/unit/test_kwork_market.py tests/unit/test_kwork_routes.py -q` passed with 57 tests; `python -m ruff check ...` passed; `npm exec vite build` passed for desktop.

## 2026-07-08 20:25 MSK catalog taxonomy API facts

Artifacts:

- `docs/kwork_catalog_taxonomy_probe_2026-07-08T164313Z.json`
- `docs/kwork_catalog_main_retry_2026-07-08T164819Z.json`
- `docs/kwork_catalog_main_auth_probe_2026-07-08T172454Z.json`

Probe context:

- VPNTE was rotated from `russia3` to `russia4` before the retry; local proxy URL stayed `http://127.0.0.1:17990`.
- Public taxonomy probes used only the installed-library mobile basic header.
- The `catalogMain` auth probe used an account token in memory only; credentials and token were not stored.
- Tavily web search found public wrappers and Kwork pages, but no reliable public documentation for `catalogMainv2`, `catalogRubrics`, or `catalogCategories`.

Live facts:

| Endpoint | Params/Auth | Live fact |
|---|---|---|
| `POST /catalogRubrics` | no params | HTTP/API success. Returns 7 root rubrics with `id`, `name`, `rubric_description`, `order`, `category_image`, `ico`, `ico_extra`. Examples: `15 Дизайн`, `11 Разработка и IT`, `5 Тексты и переводы`, `17 SEO и трафик`, `45 Соцсети и маркетинг`. |
| `POST /catalogRubrics` | tested `categoryId`, `category_id`, `rubricId`, `id` | Parameters were ignored for tested values; endpoint still returned the same 7 root rubrics. Use it as a root taxonomy endpoint, not as a filtered lookup. |
| `POST /catalogCategories` | `rubricId=11` | HTTP/API success. Returns 9 child categories with `id`, `name`, `rubric_description`, `order`, `kworks_count`. Examples: `38 Доработка и настройка сайта=13664`, `37 Создание сайта=24193`, `41 Скрипты, боты и mini apps=37914`, `79 Верстка=6509`, `80 Десктоп программирование=5201`. |
| `POST /catalogCategories` | `rubricId=15` | HTTP/API success. Returns 11 child categories. Examples include `Логотип и брендинг=46246`, `Веб и мобильный дизайн=17610`, `Арт и иллюстрации=28103`, `Полиграфия=9794`. |
| `POST /catalogCategories` | `rubricId=5/7/83` | HTTP/API success. Returns child categories with counts for text/translation, audio/video, and business/life rubrics. |
| `POST /catalogCategories` | `rubric_id`, `categoryId`, `category_id`, `parentId`, `id` | HTTP 200 but API `success=false`, error `Недостаточно параметров для метода API`. Use camelCase `rubricId` only. |
| `POST /catalogMainv2` | no token | HTTP/API success. Returns dict keys `popular_categories_block`, `catalog_service_block`, `other_services_block`. Other-services rows include useful `category_id`, often `classifier_id`, `name`, `order`. |
| `POST /catalogMain` | no token | HTTP 200 but API `success=false`, error `Необходима авторизация`. |
| `POST /catalogMain` | account token | HTTP/API success. Returns dict keys `rubrics`, `popular_categories`, `popular_kworks`, `viewed_kworks`. This is a personalized catalog-home endpoint and should be treated separately from public `catalogMainv2`. |

Practical PSR implication:

- Use `catalogRubrics` as the cheapest root rubric list.
- Use `catalogCategories(rubricId=...)` to get root -> child category taxonomy with category-level supply counts before deeper `/categoryAttributes` and `/kworks` fan-out.
- Use `catalogMainv2` for public curated market seeds; it works without account token and already contains classifier-level suggestions in `other_services_block`.
- Use authenticated `catalogMain` only when PSR wants personalized home-market signals such as `popular_kworks` and `viewed_kworks`; it is not a public replacement for `catalogMainv2`.
- Do not send `categoryId` or snake_case alternatives to `catalogCategories`; they produce successful HTTP responses with API-level errors.

## 2026-07-08 20:33 MSK account, viewed catalog, and web helper API facts

Artifact: `docs/kwork_account_misc_endpoint_probe_2026-07-08T173238Z.json`.

Probe context:

- VPNTE rotated from `russia4` to `russia5`; local proxy URL stayed `http://127.0.0.1:17990`.
- Mobile token was acquired in memory; token, password, cookies, email, and phone values were not stored.
- Session Hub returned 14 Kwork cookies for web helper endpoints.
- Seeds: competitor `kwork_id=255535`, category `41`, project seed `3213556`.

Live facts:

| Endpoint | Params/Auth | Live fact |
|---|---|---|
| `POST /getKworkDetails` | `id=255535`, mobile basic | HTTP/API success. Response is rich kwork detail with keys including `id`, `kwork_link`, `image_url`, `cover`, `default_kwork_price`, `short_user_info`, `term`, `orders_in_queue_count`, `kwork_in_favorites_count`, `kwork_title`, `kwork_description`, `category`, `badges`, `packages`, `unit_and_quantity`, `additional_options`, `portfolio_items`, `files`, `kwork_instructions`, `classifications`, `catalog_category`. This confirms it can replace competitor HTML detail loading. |
| `POST /category` | `category_id=41`, mobile basic | HTTP/API success. Compact category metadata keys: `id`, `name`, `mobile_description`, `base_volume`, `volume_type`, `attributes`, `positive_reviews_count`. |
| `POST /getKworkPortfolios` | `id=255535`, `page=1`, mobile basic | HTTP/API success, empty response list for this kwork. Treat empty as valid state, not endpoint failure. |
| `POST /actor` | token + mobile basic | HTTP/API success. Full current-account seller object with balances/counts/status fields and embedded arrays. Keys include account/profile info, `hold_amount`, `free_amount`, `currency`, unread/notify counts, `kworks_count`, `favourite_kworks_count`, `hidden_kworks_count`, `completed_orders_count`, `kworks`, `portfolio_list`, `reviews`, `worker_status`, `has_offers`, `wants_count`, `offers_count`, `archived_wants_count`, `total_amount`. |
| `POST /getActorInfo` | token + mobile basic | HTTP/API success. Compact current-account status with `id`, `username`, avatar/about/cover/profession, `total_amount`, location/timezone ids, `kworks_count`, `favourite_kworks_count`, `hidden_kworks_count`, `worker_status`, `offers_count`, `archived_wants_count`, push settings and verification flags. |
| `POST /offers` | token + mobile basic, `page=1` | HTTP/API success. Top-level keys include `success`, `response`, `connects`, `paging`; response list was empty in this account snapshot. Useful for own-offer inventory and connects state, not whole-market demand. |
| `POST /kworksStatusList` | token + mobile basic | HTTP/API success. Response list length 8. Items include `id`, `name`, `kworks_count`, `kworks`. Examples: `Активные=1`, `На модерации=0`, `Требуют исправления=0`. |
| `POST /viewedCatalogKworks` | token + mobile basic, `page=1` | HTTP/API success. Response list length 10. Items match catalog card shape and include `id`, `category_id`, `category_name`, `status_id`, `status_name`, `title`, `url`, `share_url`, `image_url`, `cover`, `price`, `worker`, `activity`, `badges`, `order`, `is_viewed`, `classification_id`. |
| `GET /api/user/checknotify` | web cookies | HTTP 200 JSON success with response keys `success`, `data`. Notification helper, low direct market value. |
| `POST /api/user/checkworkeroffers` | web cookies | HTTP 200 JSON success with keys `success`, `data`. Own worker-offer helper, useful for account state. |
| `GET /api/kwork/getkworksites?kworkId=255535` | web cookies | HTTP 200 JSON success with keys `success`, `linksSites`, `orderedLinksSites`, `html`. Can expose ordered link/site snippets for link-related kworks when present. |
| `POST /projects/check_is_template` | web cookies, JSON `description`, `wantid` | HTTP 200 JSON success with keys `success`, `data`. Pre-offer text-template classifier; useful before sending proposals, not broad market analytics. |
| `GET /wants/{wantId}/check_offer_notify` | web cookies | HTTP 200 JSON success with keys `success`, `data`. Offer notification state helper for a want. |
| `POST /api/offer/addview` | web cookies, tested `project_id` | HTTP 200 JSON with key `result`. Endpoint is live, but tested parameter did not prove market value. |

Practical PSR implication:

- `actor` and `getActorInfo` should feed account health widgets: current seller status, own kwork counts, balances, unread/notify counts, offer counts, and worker state.
- `kworksStatusList` is the cleanest source for own kwork moderation/active/paused buckets.
- `offers` is useful for own proposals and connects state; it is not a buyer-demand source.
- `viewedCatalogKworks` can be used as personalized competitor/interest history. It is account-biased, so keep it separate from whole-market public rankings.
- `getKworkDetails` is confirmed public enough for competitor detail enrichment and should replace HTML `share_url` detail fetches.
- Web helper endpoints are best kept as account-operation helpers, not core market scanners, except `getkworksites` when analyzing link/SEO kworks.

## 2026-07-08 20:40 MSK remaining isolated endpoint facts

Artifact: `docs/kwork_remaining_isolated_probe_2026-07-08T174026Z.json`.

Probe context:

- VPNTE rotated from `russia5` to `russiavless2`; local proxy URL stayed `http://127.0.0.1:17990`.
- Mobile token was acquired in memory; Session Hub returned 14 Kwork cookies.
- Own kwork seed from `/actor`: `kwork_id=51594444`, `category_id=41`.
- `POST /portfolio/log_portfolio_view` was not live-probed because it is an analytics/view counter endpoint; static JS context already showed params `portfolio_video_id`, `view_type`.

Live facts:

| Endpoint | Params/Auth | Live fact |
|---|---|---|
| `GET /api/freeprice/categorygetprices` | `categoryId=11/15`, `lang=ru` | HTTP 200 JSON `success=true`, but `prices=null` for tested root categories. |
| `GET /api/freeprice/categorygetprices` | `categoryId=41`, `lang=ru` | HTTP 200 JSON `success=true` with useful `prices`. Keys: `priceGradation`, `minPrice`, `maxPrice`, `typicalPriceGradation`, `predefinedPackageItem`. For category 41: `minPrice=500`, `maxPrice=71183`, `priceStandard` length 42 from 500 to 60000, `typicalPriceGradation` length 44 up to 70000. |
| `GET /api/freeprice/attributegetprices` | `categoryId=11&attributeId=208`, `categoryId=41&attributeId=3587`, `lang=ru` | HTTP 200 JSON `success=true`, but `prices=null` for tested attributes. |
| `POST /portfolio/load_form_orders` | web cookies, `kworkId=51594444` | HTTP 200 JSON `success=true`, `data.anotherVideos=[]`, `data.hasOrders=false`. |
| `POST /portfolio/load_form_orders_full` | web cookies, `kworkIds=51594444` | HTTP 200 JSON `success=true`, `data.orders=[]`. |
| `POST /portfolio/load_form_kworks` | web cookies, `categoryId=41`, `attributesId=3587` | HTTP 200 JSON `success=true`, `data.kworks=[]`, `data.hasOrders=false`. |
| `POST /portfolio/log_portfolio_view` | static JS only | Params from JS context: `portfolio_video_id`, `view_type`. Live mutation intentionally skipped. |

Practical PSR implication:

- `categorygetprices` is useful on leaf/working categories and should not be judged by root categories only. Use it for pricing sliders/package suggestions when `prices` is non-null.
- `attributegetprices` remains conditional; tested attributes returned null prices.
- Portfolio form loaders are not whole-market sources. They are account/portfolio publishing helpers for binding portfolio entries to own kworks/orders and can be kept out of broad market scans.
- `log_portfolio_view` should stay as a known analytics endpoint and should not be called by PSR market research.

## 2026-07-09 14:17 MSK buyer keyword suggestion endpoint

Artifact:

- `docs/kwork_market_snapshots/kwork_want_search_suggest_probe_20260709T_current.json`
- `docs/kwork_market_snapshots/kwork_want_search_suggest_post_matrix_20260709T_current.json`
- `docs/kwork_market_snapshots/kwork_buyer_scout_20260709T111729Z.json`

Live facts:

| Endpoint | Params/Auth | Live fact |
|---|---|---|
| `POST /want-search/suggest` | web Session Hub cookies, form field `query` | HTTP 200 JSON with `success` and `data.suggestions[]`. Useful for buyer keyword hints. Examples: `telegram -> Создать телеграм бота`, `python -> бот на python`, `ai -> AI агент автоматизация`. |
| `POST /want-search/suggest` | fields `keyword`, `q`, `term`, `search`, `value`, `text`, `input` | HTTP 200 but less useful/default suggestion set. Keep `query` as canonical. |
| `GET /want-search/suggest` | tested common query param names | HTTP 200 maintenance/HTML-style response, not useful for API integration. |

Practical PSR implication:

- Use `POST /want-search/suggest` as a fast buyer-market keyword hint source.
- Treat it as separate from project ranking: it helps find query windows even when `POST /projects` is temporarily unusable with cookie-only Session Hub.
- Clean all suggestion text through `_clean_text()` because Kwork can return mojibake for Cyrillic suggestions.

## 2026-07-09 14:59 MSK web buyer projects stateData

Artifacts:

- `docs/kwork_market_snapshots/kwork_web_projects_state_probe_20260709T_current.json`
- `docs/kwork_market_snapshots/kwork_web_projects_state_filter_inspect_20260709T_current.json`
- `docs/kwork_market_snapshots/kwork_web_projects_hyphen_filter_probe_20260709T_current.json`
- `docs/kwork_market_snapshots/kwork_buyer_scout_20260709T115910Z.json`

Live facts:

| Endpoint | Params/Auth | Live fact |
|---|---|---|
| `GET /projects` | web Session Hub cookies | HTTP 200 HTML with `window.stateData`. Existing parser extracts buyer projects from `pagination.data`; baseline had `67` total and `12` first-page rows. |
| `GET /projects?keyword=telegram` | web cookies | Keyword filter works. Total narrowed to `10`. |
| `GET /projects?keyword=telegram&price-to=5000&kworks-filters=0` | web cookies | Web low-budget/low-offer filter works. Total narrowed to `1`. |
| `GET /projects?keyword=python&price-to=5000&kworks-filters=0` | web cookies | Returned `1` project. |
| `GET /projects?kworks-filters=0` | web cookies | Returned `19` projects with up to 5 offers. |
| `GET /projects?c=80&kworks-filters=0&prices-filters=1` | web cookies | Category + filter combination works. Returned `1` project. |

Parameter mapping:

- `keyword`: text search. Mobile equivalent: `query`.
- `c`: category id.
- `price-to`, `price-from`: custom price bounds. Mobile equivalents: `price_to`, `price_from`.
- `kworks-filters=0`: up to 5 offers. State filter label: `До 5`.
- `prices-filters`: bucket ids from state `filters.by_budget`.
- `hiring-from`: buyer hiring percent lower bound.

Practical PSR implication:

- This is the best current source for buyer lots when Session Hub is cookie-only.
- `POST /projects` remains preferred in token-mode auth, but web `GET /projects` is fast enough and returns useful buyer lots without browser rendering.
- Use web hyphen params; underscore params like `price_to` and `kworks_filters` are ignored by the web page.
- `page` works with the web stateData source and can be used for bounded fan-out. PSR currently keeps Market scan at `project_page_limit=2` and clamps backend input to `1..3`.
- Text from web stateData and suggestions can arrive as UTF-8 or CP1251 mojibake; normalize before saving snapshots or showing market signals.
- Proven low-budget buyer discovery windows from live smoke:
  - `GET /projects?c=41&price-to=5000&kworks-filters=0`
  - `GET /projects?c=85&price-to=5000&kworks-filters=0`
  - `GET /projects?c=78&price-to=5000&kworks-filters=0`
  - `GET /projects?c=80&price-to=5000&kworks-filters=0`
- `keyword` accepts normal Unicode when sent by the application. `keyword=телеграм бот&price-to=5000&kworks-filters=0` returned `1` live lot in the 2026-07-09 smoke.

## 2026-07-09 16:35 MSK verification/captcha facts

Artifact:

- `docs/kwork_market_snapshots/kwork_verification_status_20260709T133248Z.json`

Live facts:

| Endpoint | Params/Auth | Live fact |
|---|---|---|
| `POST /getCaptchaStatus` | Session Hub cookie-only API client, `use_token=true` | Returned `KworkException: Некорректные значения параметров`. This is not reliable proof of a web captcha challenge. |
| `GET /` | Session Hub cookies | HTTP `200`, no manual verification challenge detected. |
| `GET /new` | Session Hub cookies | HTTP `200`, no manual verification challenge detected. |
| `GET /manage_kworks` | Session Hub cookies | HTTP `200`, no manual verification challenge detected. |
| `GET /projects` | Session Hub cookies | HTTP `200`, no manual verification challenge detected. |

Practical PSR implication:

- SmartCaptcha scripts can appear on normal Kwork pages. Script markers alone must not be used as `manual_verification_required`.
- Treat `getCaptchaStatus` as a weak API-side signal only. If it errors or returns true, verify against web pages before showing a captcha-required workflow.
- PSR now exposes `GET /api/kwork/verification-status` as the canonical combined diagnostic for the desktop UI.

# Kwork Autopublish Progress

Дата: 2026-07-06

## Цель

Сделать в PSR полноценный Kwork workflow:
- выбор категорий и подкатегорий как на сайте;
- сбор спроса и конкуренции по категории;
- генерация текста и обложки кворка;
- автосоздание/публикация кворка через прямые endpoints, без Playwright-first;
- проверка работоспособности и пересборка exe.

## Уже подтверждено

- Страница управления кворками: `https://kwork.ru/manage_kworks?group=active`.
- Кнопка создания ведет на `https://kwork.ru/new`.
- Создание кворка реализовано формой `form.js-kwork-save-form`.
- Основное сохранение нового кворка идет через `POST /save_kwork`.
- Редактирование существующего кворка идет через `POST /save_kwork?id=<id>`.
- Форма содержит ключевые поля: `csrftoken`, `draft_id`, `lang`, `title`, `category_id`, `description`, `instruction`, `auditory`, `service_size`, `volume`, `work_time`, `min_volume_price`, `faq`, `is_save_kwork`.
- Обложка загружается через `/temp-image-upload`.
- Атрибуты категории подгружаются через `/api/attribute/loadclassification`.
- Цены категории/атрибутов подгружаются через `/api/freeprice/categorygetprices` и `/api/freeprice/attributegetprices`.
- Основной runtime должен идти через cookies/session + `httpx`/Kwork web client. Chrome/browser нужен только для read-only разведки и аварийной диагностики.

## Что еще изучаем перед реализацией

1. Обязательные атрибуты категорий, ограничения цены и сроков.
2. Формат ответа upload обложки и как он переносится в `first_photo_json`.
3. Форматы успешных и ошибочных ответов `/save_kwork`.
4. Надежность метрик: `getWantsCount`, `/projects`, `/kworks`, public/Tavily signals.
5. Cookie-only Session Hub flow для web endpoints Kwork.
6. Статусы `/manage_kworks`: draft, moderation, active, paused, rejected.

## Черновой план реализации

1. Добавить `KworkWebListingClient` для `/new`, upload, category attributes, `/save_kwork`.
2. Добавить backend routes для категорий, метрик, draft generation, image generation, validation, publish.
3. Добавить DB models для market snapshots, listing drafts, generated assets, publish audit.
4. Добавить desktop экран Kwork publisher.
5. Добавить tests и live smoke: dry-run, потом один реальный submit только после явного подтверждения.
6. После проверки пересобрать exe.

## Уточненный план после разведки

### Категории и атрибуты

- Базовое дерево брать из `POST /categories`: верхние разделы + `subcategories`.
- Меню "как на сайте" строить из комбинации:
  - `POST /catalogRubrics` для верхних рубрик и иконок;
  - `POST /catalogMainv2` для популярных category/classifier карточек и `kworks_count`;
  - header DOM fallback со страницы Kwork, если API-структура не хватает для точного mega-menu.
- Для выбранной подкатегории грузить `POST /categoryAttributes(category_id=<id>)`.
- Хранить атрибуты как дерево `attribute_id`, `parent_id`, `title`, `alias`, `depth`, `required`, `allow_multiple`, `allow_custom`, `children`, `kworks_count`, `percent_usage`, `hint_volume`, `volume limits`.
- В UI показывать обязательные шаги выбора атрибутов. Без заполнения required path публикация недоступна.
- Для web-form совместимости дополнительно использовать `GET /api/attribute/loadclassification?categoryId=<id>&lang=ru&attributeId=<attribute_id>` и парсить input names вида `attribute[1404]`, `new_custom_attribute[1404]`.

### Метрики спроса и конкуренции

- Конкуренция:
  - основной источник: `POST /kworks(categoryId=<category_id>)` и `POST /kworks(classifierId=<classifier_id>)`;
  - поля: `response.kworks_count`, `response.classifiers`, `response.paging.total`, первая страница `kworks` с ценами, рейтингами, отзывами, badges;
  - fallback/verification: `categoryAttributes.*.kworks_count` и текст конкуренции из `/manage_kworks` карточки.
- Спрос:
  - основной источник: `POST /getWantsCount(categories=<id>)` и `POST /projects(categories=<id>)`;
  - оба требуют авторизацию, поэтому зависят от исправленного session flow;
  - из `/projects` брать свежесть, бюджет, `offers/offers_count`, `possible_price_limit`, category ids.
- Tavily:
  - использовать только `tavily_search` basic/fast, без `tavily_research`;
  - источники вроде `blog.kwork.ru` использовать как внешний сигнал по конкуренции/перспективным рубрикам;
  - не считать Tavily главным источником метрик, только пояснение и sanity-check.

### Автосоздание кворка

- `KworkWebListingClient.open_new()` делает `GET /new`, извлекает `csrftoken`, `draft_id`, форму и default hidden fields.
- `upload_cover()` делает multipart `POST /temp-image-upload`:
  - `kwork_id`: существующий id или `draftId`;
  - `category_id`: selected subcategory;
  - `validator=KworkCover`;
  - `file`;
  - `hashes[...]` для исключения дублей;
  - optional `kworkLang`.
- После upload сохранить response как `first_photo_json`; отдельно сохранить `image_path`, `image_hash`, `hash`, `kwork_id`.
- `build_save_payload()` повторяет JS-преобразование:
  - обычные поля формы;
  - `attribute[...]`;
  - `first_photo_json`;
  - `first_photo_path`;
  - `first-kwork-photo-size[]` crop JSON;
  - `faq`;
  - `is_save_kwork=1`;
  - `draft_id` для нового кворка.
- `publish()` делает `POST /save_kwork`; edit mode делает `/save_kwork?id=<id>`.
- Обработка response:
  - `result=="success"` + `redirectUrl` = опубликовано/отправлено на модерацию;
  - `code==201` = phone verification;
  - `code==298` = receipts modal;
  - `code==202` или `errors[]` = validation errors.

### Сессии

- Сейчас Session Hub отвечает `warning` и `0 cookies` для `kwork.ru`.
- `.env` содержит Kwork credentials, но token endpoints вернули "Логин или пароль указаны неверно".
- Поэтому обязательная задача реализации: cookie-only Kwork web/session flow.
- `_try_session_hub()` нельзя пропускать без `KWORK_EMAIL/KWORK_PASSWORD`; для web-only операций надо уметь создать клиент/transport из cookies.
- Для тестов и standalone scripts нужно явно грузить `.env`, иначе `KworkService` в shell не видит credentials.

### Manage/status

- `/manage_kworks?group=active` содержит карточки с `data-kwork-id`, ссылкой на публичный кворк, просмотрами, продажами, заработком, ценой и конкуренцией.
- Manage JS endpoints: `/manage_kworks/activate`, `/manage_kworks/suspend`, `/delete_kwork`, `/api/user/get_kworks_tab`, `/manage_kworks?group=<status>`.
- JS различает статусы: draft, paused, rejected, moderated, active.
- Draft edit URL строится как `/new?draft_id=<PID>`, обычный edit как `/edit?id=<PID>`.
- После publish надо проверять `/manage_kworks` или `/api/user/get_kworks_tab`, а не только ответ `/save_kwork`.

## Новые факты

- `POST /categories` работает без логина через basic auth библиотеки `kwork` и возвращает 7 верхних разделов с `subcategories`.
- `POST /categoryAttributes` требует параметр `category_id`, а не `category`, `id` или `categoryId`.
- `categoryAttributes(category_id=39)` возвращает глубокое дерево атрибутов: `required`, `allow_multiple`, `allow_custom`, `percent_usage`, `kworks_count`, `hint_volume`, `orders_inprogress_limit`, `alias`, `children`.
- `GET /api/attribute/loadclassification?categoryId=39&lang=ru` работает как XHR и возвращает JSON с HTML для формы, `selectedCount`, `count`, `disableIds`, `selectedChilds`, `attributeResolutions`.
- `GET /api/attribute/loadclassification?categoryId=39&lang=ru&attributeId=1404` возвращает следующий уровень атрибутов. В HTML имена inputs вида `attribute[1404]` и `new_custom_attribute[1404]`.
- `GET /api/freeprice/categorygetprices?categoryId=39&lang=ru` возвращает `success=true` и `prices.priceGradation`, включая допустимые цены пакетов.
- `GET /api/freeprice/attributegetprices?categoryId=39&attributeId=1404&lang=ru` вернул `prices=null` для корневого атрибута 1404, значит attribute-level цены надо проверять на выбранных leaf attributes.
- `POST /catalogFilters` принимает `category` или `id`, возвращает `filters`, `groups`, `kworks_count`. Для проверки конкуренции надо дополнительно выяснить корректный параметр выбранной рубрики: тест с `category=39` и `category=11` вернул одинаковый общий `kworks_count=783550`, вероятно без уточняющего classifier/filter.
- Tavily basic по blog.kwork.ru подтвердил, что показатель конкуренции появляется на странице создания кворка при выборе рубрики и используется Kwork как ориентир для продавца.
- `POST /projects` и `POST /getWantsCount` без токена возвращают "Необходима авторизация"; спрос по заказам требует рабочий token/session flow.
- `POST /kworks` не принимает старый параметр `category`; нужен `categoryId` или `classifierId`.
- `POST /kworks(categoryId=41)` возвращает `response.kworks_count=38215`, `kworks`, `classifiers`, `paging`.
- Для `categoryId=41` classifiers: Парсеры 2900, Чат-боты 16769, Скрипты 7555, Telegram Mini Apps 1838, ИИ-агенты 991, ИИ-боты 2921, Машинное обучение 358, IoT 190.
- `POST /kworks(classifierId=3587)` для "Чат-боты" вернул около 18401 кворков; `classifierId=3612` Telegram около 17455.
- `POST /kworks(classifierId=3934090)` Telegram Mini Apps вернул около 2168 кворков.
- `POST /kworks(classifierId=5548694)` ИИ-агенты вернул около 1627 кворков.
- `POST /kworks(classifierId=5503256)` IoT вернул около 238 кворков.
- `/kworks` sample содержит цены, title, image_url/cover, worker rating/reviews/level, badges и classifier_id; этого достаточно для конкурентного среза первой страницы.
- В `categoryAttributes(category_id=41)` найдено 122 атрибута и 17 required paths. Required branches зависят от выбранного пути: например `Тип > Чат-боты > Платформа`, `Тип > Скрипты > Вид`, `Тип > Telegram Mini Apps > Вид`.
- Upload обложки на `/temp-image-upload` строится JS-функцией для `kwork-first-photo`.
- Multipart upload обложки включает `kwork_id`, `category_id`, опционально `kworkLang`, `validator=KworkCover`, `hashes[...]` и `file`.
- Для нового кворка upload может вернуть/создать `kwork_id`, который JS кладет в `window.draftId`.
- Успешный upload кладется в `window.firstPhoto=JSON.stringify(response)` и `window.firstPhotoHash=response.hash`.
- После upload JS выставляет hidden `first_photo_path=e.image_path` и `data-hash=e.image_hash`.
- При `/save_kwork` JS добавляет `first_photo_json=JSON.parse(window.firstPhoto || "{}")`; если есть `data.src`, он также обновляет `first_photo_path`.
- Crop обложки хранится в `first-kwork-photo-size[]` как JSON с `x`, `y`, `w`, `h`, `x1`, `y1`, `x2`, `y2`, `minW`, `minH`.
- Ограничения обложки из JS: max size 10 MB, min file size 30 KB, min dimensions примерно 660x440, max dimensions 6000x10000, запрет GIF для first-photo upload.
- Ответ `/save_kwork`: при `result=="success"` происходит redirect на `redirectUrl`; код `201` открывает phone verification; код `298` открывает upload receipts; код `202`/errors показывает validation errors.
## Implementation progress 2026-07-06

- Fixed `src/platforms/kwork.py` so Session Hub cookies are no longer skipped when `KWORK_EMAIL` / `KWORK_PASSWORD` are absent. The client is created with empty login/password and injected cookies for cookie-only web/API flows.
- Updated `src/platforms/kwork_ext.py`:
  - `get_all_category_ids()` now walks nested `subcategories`, `categories`, `childs`, and `children`.
  - `get_kworks_list()` now calls `/kworks` with `categoryId` and understands `response.kworks`.
- Added `src/platforms/kwork_market.py`:
  - public category tree via `/categories`;
  - category attributes via `/categoryAttributes`;
  - price rules via `/api/freeprice/categorygetprices` and `/api/freeprice/attributegetprices`;
  - competition metrics via `/kworks` with `categoryId` / `classifierId`;
  - authenticated demand snapshot with graceful `auth_required` fallback.
- Extended `src/api/routes/kwork.py` with:
  - `GET /api/kwork/market/categories`;
  - `GET /api/kwork/market/category/{category_id}/attributes`;
  - `GET /api/kwork/market/category/{category_id}/prices`;
  - `GET /api/kwork/market/metrics`.
- Added tests:
  - `tests/unit/test_kwork_market.py`;
  - cookie-only Session Hub case in `tests/unit/test_kwork_service.py`.
- Verification command passed:
  - `$env:PYTHONPATH='C:\psr'; pytest tests\unit\test_kwork_service.py tests\unit\test_kwork_market.py -q`
  - result: `21 passed`.

## Implementation progress 2026-07-06, Chrome QA retry

- Added desktop page `desktop/src/pages/KworkMarket.tsx` and route/nav entry `/kwork-market`.
- Added desktop API methods/types for market metrics and autopublish draft/dry-run payloads.
- Added `src/platforms/kwork_autopublish.py` with guarded draft generation and dry-run save payload builder.
- Added `POST /api/kwork/autopublish/draft` and `POST /api/kwork/autopublish/publish`.
- Added `tests/unit/test_kwork_autopublish.py`.
- Verification commands passed:
  - `$env:PYTHONPATH='C:\psr'; pytest tests\unit\test_kwork_service.py tests\unit\test_kwork_market.py tests\unit\test_kwork_autopublish.py -q`
  - result: `24 passed`.
  - `cd C:\psr\desktop; npm run build`
  - result: Vite build + `electron-builder` succeeded, producing `desktop/dist-electron/PSR Desktop Setup 1.0.0.exe`.
- Chrome extension QA:
  - correct route is `http://127.0.0.1:5173/#/kwork-market`;
  - page rendered `Категории и конкуренция`;
  - selected `Разработка и IT / Скрипты, боты и mini apps` (`category_id=41`);
  - UI showed `38 226` competitors, classifiers (`Парсеры`, `Чат-боты`, `Скрипты`, `Telegram Mini Apps`, etc.), required attributes and price steps;
  - draft generation with LLM disabled worked;
  - dry-run payload showed `category_id: 41`, `min_volume_price: 1000`, title/description/instruction/faq;
  - reload after stale-metrics UX patch had no console errors.

Next:
- Add desktop API methods/types and `KworkMarket` page.
- Run live smoke against local API and Kwork endpoints.
- Add draft/autopublish endpoints after market UI is visible.

## Implementation progress 2026-07-06, chat/read-state and Kwork form manifest

- Chat fixes:
  - `src/api/routes/dashboard.py` now syncs Kwork dialogs before opening history and calls `KworkService.mark_web_dialog_read()`.
  - If numeric `project_id` cannot be marked read, dashboard retries with local `project_title` as a Kwork username candidate.
  - `src/platforms/kwork.py` added `mark_web_dialog_read(recipient)`:
    - fetches Session Hub cookies from `/cookies?domain=kwork.ru`;
    - opens `/inbox`, parses `window.chatList`;
    - opens `/inbox/{username}` to trigger Kwork web read-state;
    - falls back to `inboxRead(last_message_id)` and `markInboxTracksAsRead(dialog_id)`;
    - if chatList misses but recipient is username-like, opens `/inbox/{recipient}` directly;
    - returns structured `read_state`, including `reason=session_hub_cookies_missing` when the hub has no usable cookies.
  - `desktop/src/pages/Conversations.tsx` now has an AI draft button with the Sparkles icon. It fills the reply input only; it does not auto-send.
  - If Kwork read-state fails while opening a conversation, the UI shows a warning instead of silently pretending the site was marked read.
  - `desktop/src/lib/api.ts` added `draftConversationReply()`.
  - Added/updated unit tests in `tests/unit/test_dashboard_conversations.py` and `tests/unit/test_kwork_service.py`.
- Live smoke after restart:
  - backend PID listened on `127.0.0.1:7788`;
  - `GET /api/health` returned ok;
  - `GET /api/dashboard/conversations?limit=5` returned 2 Kwork dialogs: `22986512/Vladimir20320` and `71232/Support`;
  - `GET /api/dashboard/conversations/22986512/kwork` returned 9 messages with valid UTF-8 when read through Python `requests`;
  - read-state remained `ok=false` because current Session Hub returned `status=warning`, `count=0`, and direct Kwork API fallback logged invalid login/password. This is an environment/session problem, not a route crash.
- Verification:
  - `$env:PYTHONPATH='C:\psr'; pytest tests\unit\test_dashboard_conversations.py tests\unit\test_kwork_service.py tests\unit\test_kwork_autopublish.py tests\unit\test_kwork_market.py -q`
  - result: `37 passed`.
- Subagent Newton findings for dynamic Kwork category controls:
  - Source of truth for autopublish submit should be `/new` plus `/api/attribute/loadclassification`, not only `/categoryAttributes`.
  - Example `categoryId=41` root control names: `new_custom_attribute[208]`, `attribute[208]`; options include `3587=Chat-bots`, `3934090=Telegram Mini Apps`, `5548694=AI agents`.
  - Selecting `attributeId=3587` loads child controls including `attribute[12407]` radio and `attribute[3610][]` checkbox.
  - Selecting deeper options can load another checkbox level such as `attribute[12412][]`.
  - Submit payload for `/save_kwork` must be a list of tuples, not a dict, because repeated checkbox fields like `attribute[3610][]` must preserve duplicate keys.
  - Next implementation should add a `KworkWebListingClient`/manifest layer, parse dynamic HTML controls, cache `CategoryFormManifest`, update desktop to render real radio/checkbox controls, and extend tests for recursive controls plus `/save_kwork` response mapping.

## Implementation progress 2026-07-06, form manifest MVP

- Added `src/platforms/kwork_listing.py`:
  - `parse_classification_html(html)` parses Kwork `loadclassification` HTML into grouped controls.
  - Supports real form names like `attribute[208]`, `attribute[3610][]`, and `new_custom_attribute[208]`.
  - Preserves checkbox multiplicity instead of flattening duplicate keys.
  - `KworkWebListingClient.load_classification()` calls `/api/attribute/loadclassification`.
  - `KworkWebListingClient.build_attribute_manifest()` builds a manifest from root controls plus selected child branches.
- Added API route:
  - `POST /api/kwork/market/category/{category_id}/form-manifest`
  - request: `{ classifier_id, selection, lang }`
  - response includes `controls`, `selected`, `fragments`, `unresolved_required`.
- Updated autopublish:
  - draft now preserves `attribute_manifest` and `attribute_selection`.
  - dry-run payload now includes `form_payload` as ordered pairs, not only a dict summary.
  - `form_payload` preserves repeated fields, e.g. `attribute[3610][]=3612` and `attribute[3610][]=5273361`.
  - `form_payload` also includes base save fields, FAQ pairs, and cover upload fields when present.
- Updated desktop:
  - `desktop/src/lib/api.ts` added `KworkFormManifest` types and `getKworkFormManifest()`.
  - `desktop/src/pages/KworkMarket.tsx` now renders real Kwork form controls from manifest inside the draft panel.
  - Selecting a radio/checkbox updates `attributeSelection` and reloads child controls.
  - Draft generation sends `attribute_manifest` and `attribute_selection`.
  - Old `categoryAttributes` block is now labeled as diagnostics, not the source-of-truth form.
- Tests:
  - Added `tests/unit/test_kwork_listing.py`.
  - Updated `tests/unit/test_kwork_autopublish.py` for tuple payload and repeated checkbox fields.
  - Verification: `$env:PYTHONPATH='C:\psr'; pytest tests\unit\test_kwork_listing.py tests\unit\test_kwork_autopublish.py tests\unit\test_kwork_market.py -q`
  - result: `14 passed`.
- Desktop build:
  - `cd C:\psr\desktop; npm run build`
  - result: Vite and electron-builder succeeded; exe rebuilt at `desktop/dist-electron/PSR Desktop Setup 1.0.0.exe`.
- Live smoke without final publish:
  - backend restarted on `127.0.0.1:7788`.
  - `POST /api/kwork/market/category/41/form-manifest` with `classifier_id=3587` and `selection={"attribute[208]":3587}` returned controls:
    - `attribute[208]` radio, 8 options;
    - `attribute[12407]` radio, 2 options;
    - `attribute[3610][]` checkbox, 11 options.
  - dry-run publish with `attribute[3610][]=[3612,5273361]` returned `form_payload` pairs:
    - `["attribute[208]", "3587"]`
    - `["attribute[3610][]", "3612"]`
    - `["attribute[3610][]", "5273361"]`

Next:
- Parse/open `/new` to capture real hidden fields (`csrftoken`, `draft_id`, form action/method).
- Implement `/temp-image-upload` with generated cover and carry `first_photo_json`, `first_photo_path`, crop JSON into `form_payload`.
- Implement guarded live `/save_kwork` submission and response mapper for success, `201`, `298`, `202`, validation errors, login/csrf failures.
- Verify post-save through `/manage_kworks` or Kwork tab endpoint before calling live publish done.

## Implementation progress 2026-07-06, live save_kwork path

- Added `/new` form snapshot support in `src/platforms/kwork_listing.py`:
  - `parse_new_form_snapshot()` extracts `form_action`, `form_method`, all hidden fields, `csrftoken`, `draft_id`, and `window.draftId` fallback.
  - `KworkWebListingClient.open_new()` opens authenticated `/new` and returns a stable snapshot or login/new-form error.
- Added live upload/save helpers in `src/platforms/kwork_listing.py`:
  - `upload_cover()` posts multipart `/temp-image-upload` with `category_id`, `validator=KworkCover`, `kworkLang`, `hashes[0]`, optional `kwork_id`, and file.
  - `save_kwork()` posts `/save_kwork` as `application/x-www-form-urlencoded` bytes generated from ordered pairs.
  - Duplicate checkbox names are preserved in encoded body.
  - `parse_save_kwork_response()` normalizes `result=success`, validation errors, login/non-json responses.
- Updated `src/platforms/kwork_autopublish.py`:
  - `publish_draft(dry_run=False)` now uses Session Hub/env cookies, opens `/new`, merges hidden fields, optionally uploads cover, adds default crop JSON, and posts `/save_kwork`.
  - Removed the previous `not_implemented` live path.
  - `build_form_payload()` now preserves arbitrary hidden fields from `/new`, not only `csrftoken` and `draft_id`.
  - Generated covers are stored in draft as `cover_image_path` for later upload.
  - Added `_default_cover_crop()` center 3:2 crop helper.
- Validation findings from live smoke:
  - With ASCII-corrupted inline shell Cyrillic, Kwork returned Russian-ratio/title errors; this was a shell encoding artifact, not a browser/UI issue.
  - With ASCII-safe Unicode strings and selected `attribute[208]=3587`, `attribute[12407]=12410`, `attribute[3610][]=3612`, Kwork then required `attribute[12412]`.
  - Manifest confirmed `attribute[12412][]` language options; Python is id `12430`.
  - With `attribute[12412][]=12430`, Kwork validation only complained about missing first cover image.
  - Therefore dynamic attributes/text/price are now reaching Kwork correctly; remaining live requirement is successful cover upload and final post-save verification.
- Tests:
  - `tests/unit/test_kwork_listing.py` now covers form snapshot, save response mapping, ordered-pair encoding.
  - `tests/unit/test_kwork_autopublish.py` now covers live path with `/new` snapshot, hidden-field preservation, cover upload transfer, and crop field.
  - Verification: `$env:PYTHONPATH='C:\psr'; pytest tests\unit\test_kwork_listing.py tests\unit\test_kwork_autopublish.py tests\unit\test_kwork_market.py -q`
  - result: `19 passed`.
- Build/smoke:
  - `cd C:\psr\desktop; npm run build` succeeded.
  - Rebuilt exe: `desktop/dist-electron/PSR Desktop Setup 1.0.0.exe`.
  - Backend restarted on `127.0.0.1:7788`.
  - Safe dry-run confirmed `form_payload` includes duplicate `attribute[3610][]` and cover fields `first_photo_json`, `first_photo_path`, `first-kwork-photo-size[]`.

Next:
- Add post-save verification through `/manage_kworks` or a Kwork tab endpoint when `save_kwork` returns success.
- Do a controlled live cover upload/save with a real generated image and a user-approved final draft.
- Tighten UI preflight so live publish warns when loaded manifest controls are visible but unselected.

## Implementation progress 2026-07-06, guarded live publish

- Strengthened post-save verification in `src/platforms/kwork_listing.py`:
  - `verify_saved_kwork()` now checks `redirectUrl` plus `/manage_kworks` status groups: moderated, active, draft, paused, rejected.
  - The matcher requires title/id evidence in the same candidate card/link when both are available.
  - Page-level loose title+id matches no longer count as verified success.
- Updated live publish in `src/platforms/kwork_autopublish.py`:
  - `publish_draft(dry_run=False)` returns success only when `/save_kwork` succeeds and post-save verification succeeds.
  - If Kwork returns success but the new kwork is not found afterward, result code becomes `post_save_verification_failed`.
- Tightened desktop preflight in `desktop/src/pages/KworkMarket.tsx`:
  - Live publish is blocked while visible Kwork form controls are unselected.
  - Live publish is blocked without a generated/attached cover image.
  - Current manifest/selection are merged into the draft right before dry-run or live publish.
- Tests:
  - Added same-card verification and live verification-required coverage.
  - Verification: `$env:PYTHONPATH='C:\psr'; pytest tests\unit\test_kwork_listing.py tests\unit\test_kwork_autopublish.py tests\unit\test_kwork_market.py -q`
  - result: `22 passed`.
- Desktop build:
  - `cd C:\psr\desktop; npm run build`
  - result: Vite and electron-builder succeeded.
  - Rebuilt exe: `desktop/dist-electron/PSR Desktop Setup 1.0.0.exe`.

Next:
- Restart backend on `127.0.0.1:7788` and run safe API smoke without real publication.
- For real live publication, use a final user-approved draft with a real generated/uploaded cover, then require post-save verification before reporting success.

## Smoke 2026-07-06 20:50

- Backend restarted on `127.0.0.1:7788`, PID `20168`.
- Form manifest smoke:
  - `POST /api/kwork/market/category/41/form-manifest`
  - selection included `attribute[208]=3587`, `attribute[12407]=12410`, `attribute[3610][]=3612`, `attribute[12412][]=12430`.
  - result: `unresolved_required=[]`; controls included the main type, view, platform, and language blocks.
- Dry-run publish smoke:
  - `POST /api/kwork/autopublish/publish` with `dry_run=true`.
  - result: `ok=true`, no external Kwork save attempted.
  - `form_payload` preserved:
    - `attribute[208]=3587`
    - `attribute[12407]=12410`
    - `attribute[3610][]=3612`
    - `attribute[12412][]=12430`
    - `first_photo_json`
    - `first_photo_path`
    - `first-kwork-photo-size[]`

## Implementation progress 2026-07-06, API live confirmation gate

- Added server-side live preflight in `src/platforms/kwork_autopublish.py`:
  - live publish now requires `attribute_selection`, a loaded `attribute_manifest`, all visible manifest control values, and a cover (`cover_upload` or `cover_image_path`).
  - `publish_preflight()` returns a signed token bound to the exact draft payload hash.
  - `publish_draft(dry_run=False)` now requires the preflight token plus the confirmation phrase before cookies or Kwork network calls.
  - default confirmation phrase: `ОПУБЛИКОВАТЬ`; token TTL defaults to 600 seconds.
- Added API endpoint:
  - `POST /api/kwork/autopublish/preflight`
  - `POST /api/kwork/autopublish/publish` accepts `confirm_token` and `confirmation`.
- Updated desktop:
  - live button now calls backend preflight first.
  - if preflight succeeds, the user must type the confirmation phrase before live publish is sent.
- Tightened cover handling:
  - `/temp-image-upload` hash-only responses are no longer considered successful; an image path is required.
  - generated image API covers are saved with unique suffixes instead of overwriting by title slug.
  - generated cover bytes are normalized/validated as PNG 3:2 before upload; invalid API images fall back to local cover generation.
- Tests:
  - Added route-level coverage for token issuance and confirmation requirement.
  - Added hash-only cover upload failure coverage.
  - Verification: `$env:PYTHONPATH='C:\psr'; pytest tests\unit\test_kwork_listing.py tests\unit\test_kwork_autopublish.py tests\unit\test_kwork_market.py -q`
  - result: `25 passed`.
- Build:
  - `cd C:\psr\desktop; npm run build` succeeded.
  - Rebuilt exe: `desktop/dist-electron/PSR Desktop Setup 1.0.0.exe`.
  - timestamp: `2026-07-06 21:04:06`; size: `82192091`.
- Safe HTTP smoke:
  - backend restarted on `127.0.0.1:7788`, PID `2396`.
  - `POST /api/kwork/autopublish/preflight` returned `ok=true` and a token.
  - direct `POST /api/kwork/autopublish/publish` with `dry_run=false` and no confirmation returned `ok=false`, `code=confirmation_phrase_required`.
  - no external Kwork save was attempted in this smoke.

Next:
- Add richer dynamic-field support for Kwork custom/text fields and disabled/visibility metadata.
- Add a controlled live cover upload smoke that stops before `/save_kwork`, if possible.
- Real publish still needs a final user-approved draft and explicit typed confirmation.

## Implementation progress 2026-07-06, text/custom dynamic controls

- Extended `parse_classification_html()` in `src/platforms/kwork_listing.py`:
  - parses `textarea` controls in addition to `input` and `select`;
  - keeps text-like dynamic fields instead of dropping non-integer values;
  - keeps `new_custom_attribute[...]` as `custom_text` controls;
  - preserves `value`, `placeholder`, raw `data`, and correct `required` for boolean HTML attributes.
- Updated desktop types and UI:
  - `KworkFormControl` now exposes `value`, `placeholder`, and `data`;
  - Kwork Market renders no-option controls as text inputs;
  - entered text values are stored in `attributeSelection` and included in dry-run/live payloads.
- Tests:
  - Added coverage for text, textarea, and custom dynamic controls.
  - Verification: `$env:PYTHONPATH='C:\psr'; pytest tests\unit\test_kwork_listing.py tests\unit\test_kwork_autopublish.py tests\unit\test_kwork_market.py -q`
  - result: `26 passed`.
- Build/runtime:
  - `cd C:\psr\desktop; npm run build` succeeded.
  - Rebuilt exe: `desktop/dist-electron/PSR Desktop Setup 1.0.0.exe`.
  - timestamp: `2026-07-06 21:08:54`; size: `82193365`.
  - backend restarted on `127.0.0.1:7788`, PID `27096`.

Next:
- Use Chrome/Kwork page evidence to capture any remaining field types not represented by `input/select/textarea`.
- Add disabled/visibility metadata handling from Kwork `disableIds`, `selectedChilds`, and `attributeResolutions`.
- Run a controlled cover-upload-only smoke if a safe endpoint sequence can stop before `/save_kwork`.

## Implementation progress 2026-07-06, dynamic metadata

- Added Kwork dynamic metadata handling in `src/platforms/kwork_listing.py`:
  - flattens `disableIds` from every `/api/attribute/loadclassification` fragment;
  - marks matching options as `disabled`;
  - marks a control disabled when all options are disabled;
  - preserves top-level `metadata.disableIds`, `metadata.selectedChilds`, and `metadata.attributeResolutions`;
  - includes the raw metadata in each fragment summary.
- Updated required-field logic:
  - `unresolved_required` ignores disabled controls;
  - backend live preflight ignores disabled controls and deduplicates missing names.
- Updated desktop:
  - `KworkFormControl` includes `disabled`;
  - disabled controls are visually dimmed;
  - disabled controls/options cannot be selected and do not block local live preflight.
- Tests:
  - Added manifest metadata/disableIds coverage.
  - Added backend live preflight coverage for disabled controls.
  - Verification: `$env:PYTHONPATH='C:\psr'; pytest tests\unit\test_kwork_listing.py tests\unit\test_kwork_autopublish.py tests\unit\test_kwork_market.py -q`
  - result: `28 passed`.
- Build/runtime:
  - `cd C:\psr\desktop; npm run build` succeeded.
  - Rebuilt exe: `desktop/dist-electron/PSR Desktop Setup 1.0.0.exe`.
  - timestamp: `2026-07-06 21:18:08`; size: `82194534`.
  - backend restarted on `127.0.0.1:7788`, PID `19032`.
- Cover-upload-only smoke:
  - not executed because Session Hub currently returned `status=warning` and `0 cookies`.
  - safe `/new` check could not proceed without authenticated cookies.
  - no `/save_kwork` request was made.

Next:
- Restore fresh Kwork cookies in Session Hub, then run cover-upload-only smoke.
- Use Chrome/page evidence to capture any non-input/select/textarea controls Kwork may render for specific categories.
- Real publish still requires final user-approved draft and typed confirmation.

## Implementation progress 2026-07-06, manual env cookies and cover upload contract

- Added generic Kwork web-cookie env support in `src/platforms/kwork.py`:
  - `KWORK_WEB_COOKIES_JSON`
  - `KWORK_WEB_COOKIES_RAW`
  - `KWORK_COOKIES_JSON`
  - `KWORK_COOKIES_RAW`
  - individual env names for real Kwork cookies such as `slrememberme`, `csrf_user_token`, `uad`, `RORSSQIHEK`, `_kmid`, `_kmfvt`, `_kmwl`, `userId`, `PHPSESSID`.
- `KworkService._fetch_session_hub_cookies()` now falls back to env cookies when Session Hub is unavailable or returns a non-ok status.
- `KworkAutopublishService._env_web_cookies()` now uses the shared parser instead of its old narrow `rememberme/USERID/csrftoken` mapping.
- Added safe endpoint:
  - `GET /api/kwork/autopublish/session-check`
  - opens `/new` and returns form/session state without exposing cookie values.
- Live manual cookie smoke:
  - Yandex cookie set was supplied through temporary process env only; not written to repo or progress docs.
  - `/new` opened successfully.
  - result: `ok=true`, final URL `/new`, csrf present, hidden fields count `13`.
- Cover-upload-only smoke:
  - generated a local PNG cover;
  - posted only `/temp-image-upload`;
  - did not call `/save_kwork`;
  - first attempt revealed real Kwork response shape: `{ success: true, data: { id, name, src, hash } }`.
- Fixed upload contract in `src/platforms/kwork_listing.py`:
  - nested `data.name` is now used as `first_photo_path`;
  - nested `data` is now used as `first_photo_json`;
  - nested `data.hash` is now used as `first_photo_hash`.
- Re-ran cover-upload-only smoke:
  - `upload_ok=true`;
  - `first_photo_path` present;
  - `first_photo_json` present;
  - hash present;
  - no draft id returned;
  - no `/save_kwork` request made.
- Tests:
  - Added env cookie parser coverage.
  - Added nested Kwork cover upload response coverage.
  - Verification: `$env:PYTHONPATH='C:\psr'; pytest tests\unit\test_kwork_service.py tests\unit\test_kwork_listing.py tests\unit\test_kwork_autopublish.py tests\unit\test_kwork_market.py -q`
  - result: `51 passed`.
- Build/runtime:
  - `cd C:\psr\desktop; npm run build` succeeded.
  - Rebuilt exe: `desktop/dist-electron/PSR Desktop Setup 1.0.0.exe`.
  - timestamp: `2026-07-06 21:52:53`; size: `82196420`.
  - backend restarted on `127.0.0.1:7788`, PID `25992`, with temporary Kwork env cookies inherited by that process.

Next:
- Use the app to generate a final draft with cover, inspect payload/preflight, then only publish after explicit typed confirmation.
- Consider a small UI indicator for `/autopublish/session-check`.

## Implementation progress 2026-07-06, real live publish verified

- Fixed the live cover save contract after comparing Kwork browser JS behavior:
  - `/temp-image-upload` response is preserved as full `first_photo_json`;
  - `first_photo_path` uses nested `data.src`;
  - live `/save_kwork` now sends a structured JSON payload converted from browser-style field names such as `attribute[3610][]`, `faq[0][question]`, and `first-kwork-photo-size[]`;
  - `first-kwork-photo` is sent as JSON `null` in the structured payload, not as an uploaded file string.
- Added tests for the structured Kwork save payload and JSON save request.
- Verification:
  - `python -m pytest tests/unit/test_kwork_listing.py tests/unit/test_kwork_autopublish.py tests/unit/test_kwork_service.py`
  - result: `49 passed`.
  - `python -m pytest tests/unit`
  - result: `131 passed`.
- Full `python -m pytest` still collects unrelated external/manual tests and fails before running the suite:
  - `_external/tools/autopentest-ai/.../test_all_endpoints.py` expects a missing `js_endpoints.txt`;
  - `tests/manual/test_flru_endpoint.py` runs a live Playwright manual check during collection.
  - This is pre-existing collection behavior, not a Kwork unit regression.
- Real publish smoke:
  - used temporary env cookies only; no secrets were written to repo/docs;
  - opened authenticated `/new`;
  - uploaded cover;
  - submitted live `/save_kwork`;
  - Kwork returned `result=success`;
  - post-save verification opened the returned Kwork URL and matched the created title.
- Created and verified Kwork:
  - title: `Создам бота для заявок и уведомлений`;
  - URL: `https://kwork.ru/script-programming/53342413/sozdam-bota-dlya-zayavok-i-uvedomleniy`.
- Hardened API route `/api/kwork/autopublish/publish` so unexpected live exceptions return JSON `publish_exception` instead of a raw HTTP 500.

Next:
- Rebuild desktop exe after the route hardening.
- Optionally add a visible "Kwork session OK" indicator in the desktop UI.

## Final verification 2026-07-06

- Rebuilt desktop installer after the last backend fixes:
  - `C:\psr\desktop\dist-electron\PSR Desktop Setup 1.0.0.exe`
  - timestamp: `2026-07-06 22:42:58`
  - size: `82199966`
- Restarted backend on `127.0.0.1:7788`, PID `1480`.
- Safe smoke:
  - `GET /api/health` returned ok;
  - `GET /api/kwork/autopublish/session-check` returned ok and form action `https://kwork.ru/save_kwork`;
  - incomplete live publish request returned JSON `live_preflight_failed`, not HTTP 500.
- Public created Kwork URL returned HTTP 200 and contained the created title.

## Cover generation fix 2026-07-06

- Fixed bad cover text handling:
  - post-processing text overlay is now disabled by default;
  - overlay is allowed only when request has explicit `cover_text_overlay=true`;
  - image-generation AI is still allowed and instructed to render the required Russian offer text inside the image itself.
- Added real competitor-cover vision analysis before image prompt generation:
  - competitor `image_url` values are extracted from market context;
  - up to `KWORK_COMPETITOR_COVER_LIMIT` images are downloaded and compressed to data URLs;
  - LLM router now supports multimodal `generate_with_images(...)` through OpenAI-compatible `chat/completions`;
  - cover prompt generation receives a `competitor_visual_analysis` brief based on actual pixels, not only URL strings;
  - draft image result exposes `visual_analysis_status`, `competitor_images_seen`, and optional visual brief/detail.
- Frontend:
  - draft request now sends `use_competitor_image_analysis` when cover generation is enabled;
  - cover preview shows a compact indicator with how many competitor covers were analyzed.
- Safety:
  - no Kwork was published during this fix;
  - no secrets/cookies were written to this document.
- Verification:
  - `python -m pytest tests/unit/test_llm_router.py tests/unit/test_kwork_autopublish.py` -> `61 passed`;
  - `python -m pytest tests/unit` -> `135 passed`;
  - `cd C:\psr\desktop; npm run build` succeeded;
  - rebuilt installer: `C:\psr\desktop\dist-electron\PSR Desktop Setup 1.0.0.exe`, timestamp `2026-07-06 23:20:23`, size `82209826`;
  - restarted backend on `127.0.0.1:7788`, PID `15688`;
  - `GET /api/health` returned ok;
  - `GET /api/kwork/autopublish/session-check` returned ok with form action `https://kwork.ru/save_kwork`;
  - safe draft smoke returned ok without generating an image or publishing;
  - market metrics smoke for category `38` returned competitors with cover image URLs.

## Session Hub and dialogs fix 2026-07-07

- Session Hub in `C:\pechenki\session_hub`:
  - `/cookies` now returns one best whole browser/profile cookie group by default instead of mixing cookies from multiple profiles;
  - added a short TTL cache with `refresh=true` override;
  - expired Chrome/Yandex cookies are filtered out;
  - WAL/SHM sidecar files are copied with the cookie DB snapshot so fresh browser writes are visible;
  - `/health` now works and reports cache entry count;
  - `build.bat` now uses `python -m pip` / `python -m PyInstaller`, requests admin manifest, and exits on build errors.
- PSR Kwork integration:
  - Session Hub responses with warning status but valid cookies are accepted;
  - cookie-sensitive Kwork operations refresh cookies before use;
  - `/api/kwork/status` treats a live Session Hub cookie session as `api_ok=true` and no longer fails only because password auth is stale.
- Dialogs:
  - Kwork inbox sync is used before returning conversations/history;
  - support dialogs are marked as `system`;
  - customer dialogs with zero unread are marked as `read`, not `awaiting_reply`;
  - opening a Kwork dialog attempts to mark it read on the site;
  - desktop conversations poll for new messages, show notices/notifications, can send replies through the conversation endpoint, and can request an AI reply draft.
- Verification:
  - `python -m pytest tests/unit` -> `138 passed`;
  - `cd C:\psr\desktop; npm run build` succeeded;
  - rebuilt installer: `C:\psr\desktop\dist-electron\PSR Desktop Setup 1.0.0.exe`, timestamp `2026-07-07 00:30:02`, size `82211859`;
  - backend restarted on `127.0.0.1:7788`, PID `10336`;
  - Session Hub is live on `127.0.0.1:8669`, PID `1424`;
  - `GET /api/kwork/status` returned `api_ok=true`, auth mode `email+cookies`;
  - `GET /api/kwork/autopublish/session-check` returned ok and found the Kwork creation form;
  - `GET /api/dashboard/alerts` returned count `0`;
  - `GET /api/dashboard/conversations?limit=20` returned 2 dialogs: `Vladimir20320:read` and `Support:system`.
- Note:
  - the current Session Hub exe at `C:\pechenki\session_hub\dist\session_hub.exe` was rebuilt earlier in this iteration and is running;
  - a later rebuild attempt could not overwrite the same file while the elevated exe was running, so the build script was hardened to report that error cleanly.

## Follow-up 2026-07-07, active defect list

User-verified issues to fix next:

1. Some Kwork autopublish/API path still returns a raw `internal server error`.
2. Market filters are incomplete: besides category/classifier there are deeper filters/attributes, and demand/orders context must respect the exact selected filter slice before it is given to the AI.
3. `Kwork просит проверку на бота` can be a false positive. The app needs a better detector and a manual verification surface inside the app, not a blind link that opens Kwork as a normal human page.
4. Kwork form questions/labels are parsed wrong in some categories: option text such as `Макросы для Office` can be shown as the field question/header too.
5. Cover prompt generation still feels fixed. The prompt writer must use the actual competitor covers plus recent generated covers/prompts as visual context, so it can deliberately vary composition and style for the current service/filter context.

Constraints:
- Do not automate or bypass CAPTCHA. Only support user-driven manual verification in an app-owned browser/webview.
- Use Tavily search/extract only as an auxiliary source; do not use Tavily research.
- Do not hardcode category/filter numbers as the solution.
- Keep this file updated before context-heavy work.

## Follow-up implementation 2026-07-07

Done in this pass:

- Kwork form manifest now uses Session Hub cookies and returns structured `success=false` payloads for non-JSON/login/error cases instead of leaking raw HTTP 500.
- Manual Kwork verification detection is stricter:
  - backend recognizes Yandex SmartCaptcha markers including `smart-captcha`, `smartcaptcha.cloud.yandex.ru`, and `smart-token`;
  - detector logs a sanitized `Kwork manual_verification_required` warning so the existing desktop log WebSocket can trigger a global banner/notification;
  - frontend log detector suppresses false positives such as `captcha_required=false` and generic health noise.
- Manual verification is user-driven only:
  - global app banner and Logs page can open the sandboxed Electron Kwork verification window;
  - Kwork Market form-manifest and live-publish failures show an inline button to open the same window;
  - no CAPTCHA bypass/solver was added.
- Kwork dynamic field labels:
  - parser no longer treats option labels as field questions;
  - real nearby group labels still become `question`.
- Market metrics:
  - desktop sends POST `/api/kwork/market/metrics` with current `attribute[...]` selection and parsed controls;
  - backend computes effective `classifierId` values from selected dynamic options and fans out `/kworks` calls for multi-select slices;
  - demand snapshot receives the same filters plus compatibility `classifierId/attr`;
  - response exposes `filter_scope` and `filter_requests` for UI transparency and LLM context.
- Cover prompt generation:
  - prompt writer context now includes selected market slice, demand, filter requests, competitor visual analysis, and recent generated cover history;
  - prompt writer route is explicit: `llm_vision`, `llm_text_no_images`, `llm_text_after_vision_failure`, or fallback;
  - UI shows cover diagnostics: route, competitor images sent, recent cover images sent, prompt image count, sidecar path, and warnings;
  - sidecars store `cover_prompt_context`;
  - orphan generated PNGs without sidecars are included in recent cover history;
  - local fallback cover seed includes prompt/context so repeated similar drafts are less fixed.

Verification:

- `python -m pytest tests\unit\test_kwork_listing.py tests\unit\test_kwork_routes.py tests\unit\test_kwork_market.py tests\unit\test_kwork_autopublish.py -q` -> `50 passed`.
- `python -m pytest tests\unit -q` -> `154 passed`.
- `cd C:\psr\desktop; npm run build` succeeded.
- Rebuilt installer: `C:\psr\desktop\dist-electron\PSR Desktop Setup 1.0.0.exe`, timestamp `2026-07-07 14:05:24`, size `82241252`.
- Tavily extract was used only for official Yandex SmartCaptcha docs to confirm manual widget/token markers; no research agent mode was used.

## Follow-up implementation 2026-07-07, deeper slices and Kwork load shedding

Done in this pass:

- Kwork Market slice UI:
  - `Срез внутри рубрики` now continues into dynamic Kwork form filters when Kwork has no more classifier children;
  - the old `final slice` notice is shown only when there are no classifier children and no selectable dynamic controls;
  - classifier choices try to synchronize with matching `attribute[...]` option ids, so competitors/demand use the same exact slice;
  - changing classifier no longer blindly resets `attributeSelection`.
- Frontend request pressure:
  - metric refresh uses debounced `attributeSelection`/manifest controls instead of recalculating on every checkbox click;
  - form manifest reload is debounced and stale manifest responses are ignored;
  - shared `useApi` ignores stale responses from older overlapping requests.
- Backend request pressure:
  - market metrics have a live-client TTL/in-flight cache for identical slices;
  - multi-select classifier fan-out is capped by `KWORK_MARKET_MAX_FANOUT` (default `2`);
  - competitor HTML detail enrichment default was reduced from 6 to 2;
  - competitor detail page fetches are sequential, delayed by `KWORK_COMPETITOR_DETAIL_DELAY` (default `0.8s`), and cached by URL;
  - Session Hub cookie fetches now have a TTL (`KWORK_SESSION_HUB_COOKIE_TTL`, default `90s`) and in-flight dedupe.
- Cover image API:
  - `/images/generations` now retries compatible fallback payloads on `400/422` (`gpt-image-1`, then `1024x1024`/`standard`) before using local fallback;
  - local fallback detail includes short image API response text from failed attempts.

Verification:

- `python -m pytest tests\unit\test_kwork_market.py tests\unit\test_kwork_routes.py tests\unit\test_kwork_service.py -q` -> `38 passed`.
- `python -m pytest tests\unit -q` -> `157 passed`.
- `cd C:\psr\desktop; npm run build` succeeded.
- Rebuilt installer: `C:\psr\desktop\dist-electron\PSR Desktop Setup 1.0.0.exe`, timestamp `2026-07-07 18:09:11`, size `82248003`.

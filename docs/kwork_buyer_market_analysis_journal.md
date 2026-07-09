# Kwork Buyer Market Analysis Journal

## 2026-07-09 01:10 MSK - backend-only market run

Purpose: run market analysis from PSR backend code without desktop UI/browser parsing.

Artifacts:

- `docs/kwork_market_snapshots/backend_market_analysis_20260708T220837Z.json`
- `docs/kwork_market_snapshots/backend_buyer_query_probe_20260708T221009Z.json`

Method:

- Used `KworkMarketClient.get_market_metrics(...)` for supply/competitor slices.
- Used `KworkService.get_wants_count(...)` and `KworkService.get_raw_projects(...)` for buyer/order demand.
- Competitor details were disabled, so no per-card detail HTML/API enrichment was involved.
- Public Tavily check found mostly seller kwork pages and public wrappers; buyer/order intelligence came from mobile API endpoints, not indexed public pages.
- Autopentest WSTG tracking: `WSTG-INFO-06`, engagement `psr-kwork-buyer-api-2026-07-09`.

Confirmed buyer/order API entry points:

- `POST https://api.kwork.ru/getWantsCount`
  - Useful params: `categories`, `query`, `kworks_filter_to`, `kworks_filter_from`, `price_from`, `price_to`.
  - Role: cheap count before fetching project cards.
- `POST https://api.kwork.ru/projects`
  - Useful params: `categories`, `page`, `query`, `kworks_filter_to`, `kworks_filter_from`, `price_from`, `price_to`, `hiring_from`.
  - Role: actual buyer/order list with paging, offers, price, category, buyer stats and description.

Supply/demand result from backend run:

| Slice | Supply kworks | Buyer wants | Wants per 1000 kworks | Sample avg price |
|---|---:|---:|---:|---:|
| Telegram platform in bots (`category_id=41`, `classifier_id=3612`) | 17,396 | 43 | 2.472 | 2,450 |
| Chat-bots type (`category_id=41`, `classifier_id=3587`) | 18,258 | 43 | 2.355 | 2,450 |
| Telegram Mini Apps (`category_id=41`, `classifier_id=3934090`) | 2,160 | 43 | 19.907 | 24,700 |
| Programming broad (`category_id=41`) | 38,111 | 43 | 1.128 | 7,650 |
| Website creation (`category_id=37`) | 24,155 | 29 | 1.201 | 12,850 |
| Website maintenance/setup (`category_id=79`) | 6,487 | 7 | 1.079 | 1,650 |

Buyer query result:

| Query | Wants | Sample | Useful signal |
|---|---:|---:|---|
| `telegram` | 39 | 12 | Broad Telegram demand; includes mini app, stories, CRM and bot tasks. |
| `телеграм бот` | 59 | 12 | Stronger direct buyer query than plain `telegram`. |
| `бот` | 29 | 12 | Mixed bot tasks; less precise but still useful. |
| `автоматизация` | 19 | 12 | Smaller but higher-value automation demand. |
| `python` | 5 | 5 | Low volume, but tightly technical. |
| `сайт` | 100 | 12 | Very broad and noisy. |
| `доработка сайта` | 100 | 12 | Large buyer pool, but many samples already have high offer counts. |
| `bitrix` | 3 | 3 | Tiny but specific. |
| `wordpress` | 26 | 12 | Useful site-maintenance niche. |

Low-offer buyer filters:

| Query + `kworks_filter_to=5` | Wants | Examples |
|---|---:|---|
| `telegram` | 17 | Telegram Stories mentions, Mini App buyers, CRM/bot adjacent work. |
| `телеграм бот` | 23 | Bot promotion, Telegram Stories, Mini App buyer tasks. |
| `сайт` | 23 | WordPress sections, client search for sites, site promotion/consulting. |
| `автоматизация` | 7 | Bitrix24 setup, client search, cold outreach, automation prototype tasks. |

Interpretation:

- The UI line like `OlegTixomirov - 2 cards` is not market analysis. It is only seller frequency inside a tiny sampled competitor card set. It can stay as a small debug/snapshot chip, but should not be presented as the main market result.
- The real market analysis should prioritize buyer demand:
  - count by query/category,
  - low-offer count,
  - sample buyer lots,
  - offer count distribution,
  - budgets,
  - supply-to-demand ratio,
  - whether higher price is allowed.
- For the current Telegram bot/automation direction, the most actionable search is `телеграм бот` plus `kworks_filter_to=5`. It returned 23 low-offer wants in this run.
- `Telegram Mini Apps` is the best-looking supply/demand slice by density in this run: only 2,160 supply cards versus 43 category demand items, and a much higher sample competitor price.
- `доработка сайта` has many buyer lots, but the raw query is broad and crowded. Use more specific query/facet filters such as `wordpress`, `bitrix`, `modx`, `верстка`, `правка`, and low-offer filter.

UI/product notes:

- Replace the central market headline with buyer-oriented metrics first: `buyer wants`, `low-offer wants`, `avg budget`, `min offers`, `supply kworks`, `demand per 1000 kworks`.
- Move seller frequency chips under a label like `sample seller frequency`, not `market analysis`.
- Add a buyer-lot panel from `/projects` samples with title, price, offers, higher-price flag and category.
- Add preset query buttons for current draft text: `телеграм бот`, `автоматизация`, `python`, `wordpress`, `bitrix`.
- Keep the current fast mode: no competitor details unless the user explicitly enables descriptions.

## 2026-07-09 01:28 MSK - manual verification diagnostic and fix

Purpose: investigate why PSR said Kwork required manual verification while the user did not see a captcha window.

Artifacts:

- `docs/kwork_manual_verification_diagnostic_20260708T221952Z.json`
- `docs/kwork_manual_verification_raw_diagnostic_20260708T222158Z.json`
- `docs/kwork_manual_verification_after_fix_20260708T222815Z.json`

Findings:

- The app path is:
  - backend detects `manual_verification_required`;
  - desktop extracts `final_url`;
  - Electron imports Session Hub cookies into a dedicated Kwork verification partition;
  - Electron opens the supplied Kwork URL.
- Electron is able to open the backend `final_url`; the window helper itself is not hardcoded to `/new`.
- The old backend detector was too broad:
  - one regex mixed strong SmartCaptcha markers with weak Russian phrases like `автоматические скрипт` and `большой нагрузк`;
  - therefore a normal `200 https://kwork.ru/new` page could be treated as manual verification if it contained generic anti-bot text.
- Current live state after repeated checks is a real challenge:
  - `GET https://kwork.ru/new` redirects/lands on `https://kwork.ru/not_access.php`;
  - HTTP status: `403`;
  - strong markers: `smartcaptcha`, `smart-captcha`, `data-sitekey`;
  - weak markers: `подтвердите, что вы не робот`, `автоматические скрипт`, `большой нагрузк`;
  - normal publish form markers: absent.
- `KworkService.get_captcha_status()` returned `false` in the earlier diagnostic, so it is not sufficient by itself to decide whether `/new` is currently blocked.

Code change:

- `src/platforms/kwork_listing.py`
  - split manual verification detection into strong markers, weak markers, challenge URL/status, and normal new-form markers;
  - `200 /new` with a normal save form is no longer treated as captcha just because weak anti-bot text exists;
  - `403 /not_access.php` with SmartCaptcha/robot text is still treated as `manual_verification_required`;
  - response now includes `evidence` for future diagnostics.
- `tests/unit/test_kwork_listing.py`
  - added regression tests for weak text on normal `/new`;
  - added regression tests for weak challenge text on `403 /not_access.php`.

Verification:

- `python -m pytest C:\psr\tests\unit\test_kwork_listing.py C:\psr\tests\unit\test_kwork_autopublish.py -q`
- Result: `40 passed in 1.38s`.

Autopentest tracking:

- `WSTG-INFO-07`: completed for the manual-verification execution path.
- `WSTG-SESS-01`: in progress for Session Hub cookie import and Kwork verification partition behavior.

Operational note:

- If PSR reports `manual_verification_required` with `final_url=https://kwork.ru/not_access.php` and evidence includes `smart-captcha`/`data-sitekey`, the captcha is real and the verification window must open that final URL.
- If PSR reports `manual_verification_required` with `final_url=https://kwork.ru/new`, check the new `evidence.has_new_form` and matches. After this fix, normal `/new` form plus weak text should no longer block as captcha.

## 2026-07-09 01:44 MSK - buyer scout live probe after proxy rotation

Purpose: build a buyer-first scouting model instead of seller-frequency chips.

Artifacts:

- Failed pre-rotation probe: `docs/kwork_buyer_scout_live_probe_20260708T223839Z.json`
- Successful post-rotation probe: `docs/kwork_buyer_scout_live_probe_20260708T224254Z.json`

Proxy/runtime note:

- Pre-rotation mobile sign-in failed with `HTTP 403 for POST /signIn` and current exit IP.
- VPNTE rotation via `rotate_vpnte_proxy_if_enabled()` changed profile from `norwayvless1` / Norway to `germanyvless1` / Germany.
- After rotation, `getWantsCount(categories=all, query=telegram, kworks_filter_to=5)` recovered and returned `16`.

Buyer scout method:

- Read-only API calls only.
- Probe set:
  - `query=телеграм бот`, `kworks_filter_to=5`
  - `query=telegram`, `kworks_filter_to=5`
  - `query=автоматизация`, `kworks_filter_to=5`
  - `query=wordpress`, `kworks_filter_to=5`
  - `categories=41`, `kworks_filter_to=5`
  - `query=доработка сайта`, `kworks_filter_to=10`
  - `query=телеграм бот`, `kworks_filter_to=0`
  - `query=сайт`, `kworks_filter_to=0`
- Dedupe by project id.
- Score lots by:
  - lower offer count;
  - budget / `possible_price_limit`;
  - `allow_higher_price`;
  - buyer hiring history;
  - detailed brief;
  - penalty for crowded offers or already-work flags.

Successful scout result:

| Metric | Value |
|---|---:|
| Unique projects sampled | 46 |
| Projects with 0 offers | 10 |
| Projects with <=5 offers | 43 |
| Top score | 94 |
| Total probe time | 25.8s |

Probe counts:

| Probe | Count | Sample |
|---|---:|---:|
| `телеграм бот`, <=5 offers | 22 | 12 |
| `telegram`, <=5 offers | 16 | 12 |
| `автоматизация`, <=5 offers | 7 | 7 |
| `wordpress`, <=5 offers | 4 | 4 |
| category `41`, <=5 offers | 7 | 7 |
| `доработка сайта`, <=10 offers | 28 | 12 |
| `телеграм бот`, 0 offers | 5 | 5 |
| `сайт`, 0 offers | 6 | 6 |

Top examples from this run:

| ID | Score | Offers | Budget | Possible max | Probe | Title |
|---:|---:|---:|---:|---:|---|---|
| 3202311 | 94 | 0 | 30000 | 90000 | `телеграм бот` <=5 | `Нужно создать 5-6 Telegram Stories с упоминанием` |
| 3211575 | 94 | 0 | 40000 | 120000 | `телеграм бот` <=5 | `Поиск клиентов постоянно` |
| 3192226 | 94 | 0 | 30000 | 90000 | `телеграм бот` <=5 | `Поиск заказов` |
| 3204499 | 94 | 0 | 30000 | 90000 | `телеграм бот` 0 offers | `10000 упоминаний в telegram` |
| 3212022 | 87 | 1 | 10000 | 30000 | `telegram` <=5 | `Массовый инвайтинг +рассылка с отчетом` |
| 702238 | 87 | 2 | 30000 | 90000 | `доработка сайта` <=10 | `Комплексное продвижение сайта` |

Endpoint facts confirmed:

- `POST https://api.kwork.ru/getWantsCount`
  - Params used: `categories`, `query`, `kworks_filter_to`.
  - Role: fastest preflight count for buyer lots.
- `POST https://api.kwork.ru/projects`
  - Params used: `categories`, `page`, `query`, `kworks_filter_to`.
  - Role: list of buyer lots; fields include `id`, `title`, `description`, `price`, `possible_price_limit`, `offers`, `allow_higher_price`, buyer stats.
- `POST https://api.kwork.ru/project`
  - Params used: `id`.
  - Response shape: dict.
  - Useful fields seen: `id`, `title`, `description`, `price`, `possible_price_limit`, `offers`, `has_offer`, `is_viewed`, `time_left`, `username`, `user_id`, `user_projects_count`, `user_active_projects_count`, `user_hired_percent`, `achievements_list`.
  - Best for actionable card/detail before deciding to answer.
- `POST https://api.kwork.ru/want`
  - Params used: `id`.
  - Response shape in this run: `response` is a one-item list.
  - Useful fields seen: `id`, `title`, `description`, `price_limit`, `possible_price_limit`, `offers`, `orders`, `views`, `views_history`, `date_create`, `date_active`, `date_expire`, `status`, `want_status_id`.
  - Best for demand history and view/order analytics.

Important mismatch:

- For project `3202311`, `/project` reported `offers=0`, while `/want` reported `offers=11`.
- Treat `/projects` and `/project` as the current actionable exchange-card view.
- Treat `/want` as the richer want analytics view; do not blindly overwrite actionable offer count from `/projects` with `/want.offers`.

Practical buyer-search recipe:

1. Call `/getWantsCount` for many cheap query/filter combinations.
2. Prioritize combinations with non-zero count and low offer filters.
3. Fetch `/projects` only for top combinations.
4. Dedupe by `id`.
5. Score lots by low offers, budget, higher-price flag, buyer history and brief quality.
6. Fetch `/project` for top lots before drafting an offer.
7. Fetch `/want` only when analytics are needed: views, order count, history, active/expire dates.

Recommended next implementation:

- Add a first-class `buyer scout` backend route that returns the ranked list above.
- UI should show top buyer lots before seller frequency.
- Keep seller-frequency chips as secondary debug context only.

## 2026-07-09 02:05 MSK - buyer scout backend route

Purpose: convert the one-off buyer scout probe into reusable PSR backend functionality.

Implemented:

- `src/platforms/kwork_market.py`
  - Added `DEFAULT_BUYER_SCOUT_PROBES`.
  - Added `KworkMarketClient.score_buyer_project(...)`.
  - Added `KworkMarketClient.fetch_project_detail(...)`.
  - Added `KworkMarketClient.get_buyer_scout(...)`.
- `src/api/routes/kwork.py`
  - Added `KworkBuyerScoutRequest`.
  - Added `POST /api/kwork/market/buyer-scout`.
- `desktop/src/lib/api.ts`
  - Added `getKworkBuyerScout(...)`.
  - Added `KworkBuyerScoutRequest` and `KworkBuyerScout` interfaces.
- Tests:
  - `tests/unit/test_kwork_market.py`
  - `tests/unit/test_kwork_routes.py`

Route contract:

`POST /api/kwork/market/buyer-scout`

Request fields:

- `probes`: optional list of `{name, categories, query, kworks_filter_to, ...}`.
- `max_probes`: default `8`.
- `page`: default `1`.
- `per_probe_limit`: default `12`.
- `top_limit`: default `20`.
- `include_project_details`: default `true`.
- `include_want_details`: default `true`.
- `detail_limit`: default `8`.
- `write_file`: default `false`.

Response fields:

- `probes`: per-probe counts and samples.
- `top`: ranked buyer lots.
- `aggregate`: `probe_count`, `unique_projects`, `zero_offer_count`, `low_offer_count`, `top_score`.
- `endpoint_errors`: non-fatal per-probe errors.
- `timings_ms`.

Ranking rules:

- Strong boost for `0 offers`, `1-2 offers`, `<=5 offers`.
- Budget boost from `price` and `possible_price_limit`.
- Extra boost for `allow_higher_price`, buyer hiring history, portfolio request, detailed brief.
- Penalty for crowded offers and `already_work`.

Verification:

- `python -m pytest C:\psr\tests\unit\test_kwork_market.py C:\psr\tests\unit\test_kwork_routes.py -q`
  - Result: `36 passed in 1.03s`.
- `python -m ruff check C:\psr\src\platforms\kwork_market.py C:\psr\src\api\routes\kwork.py C:\psr\tests\unit\test_kwork_market.py C:\psr\tests\unit\test_kwork_routes.py`
  - Result: `All checks passed`.
- `npx tsc --noEmit --pretty false` in `desktop`
  - Failed on pre-existing desktop typing issues in `Conversations`, `Conversion`, `Dashboard`, `Health`, `Orders`, `Skipped`, and an existing `KworkMarket.cleanCompetitorText` inference issue.
  - No new `api.ts` buyer-scout-specific TypeScript error was reported.

Autopentest tracking:

- Added graph node: `POST /api/kwork/market/buyer-scout`.
- Updated `WSTG-APIT-02` with the local PSR REST wrapper around the confirmed Kwork buyer APIs.

Next implementation step:

- Wire buyer scout into `KworkMarket.tsx` as the main market-scan result:
  - show ranked buyer lots first;
  - show seller-frequency chips only as secondary sample context;
  - expose presets for `телеграм бот`, `telegram`, `автоматизация`, `wordpress`, `доработка сайта`.

## 2026-07-09 02:36 MSK - buyer API filter matrix

Purpose: verify buyer-search filter behavior against live Kwork API without the UI.

Artifacts:

- `docs/kwork_buyer_api_filter_matrix_20260709.md`
- `docs/kwork_market_snapshots/kwork_buyer_filter_matrix_germany_20260708T233600Z.json`

Confirmed endpoints:

- `POST /projects`
- `POST /getWantsCount`

Confirmed useful params:

- `categories`
- `page`
- `query`
- `price_from`
- `price_to`
- `hiring_from`
- `kworks_filter_from`
- `kworks_filter_to`

Live findings on `germanyvless1`:

- `telegram`, `kworks_filter_to=5`: count `16`, sample `12`.
- `telegram`, `kworks_filter_to=0`: count `3`, sample `3`.
- `telegram`, `price_from=30000`, `kworks_filter_to=5`: count `11`, sample `11`.
- `telegram`, `hiring_from=80`, `kworks_filter_to=5`: count `0`.
- `telegram`, `kworks_filter_to=5`, `sort=date`: same count and same first ids as default, so no proven advantage.
- `телеграм бот`, `kworks_filter_to=5`: count `22`, sample `12`.
- `сайт`, `kworks_filter_to=0`: count `6`, sample `6`.
- `доработка сайта`, `kworks_filter_to=10`: count `28`, sample `12`.

Proxy note:

- `russia` and bridge profiles timed out on buyer API during this run.
- Explicit rotate to `germanyvless1` restored normal responses.

Autopentest tracking:

- Added graph nodes:
  - `POST /projects`
  - `POST /getWantsCount`
- Tracked `WSTG-APIT-02` as completed for read-only REST API inventory and parameter behavior probe.

## 2026-07-09 02:45 MSK - buyer scout default probes updated

Purpose: bake the live filter-matrix finding into the reusable backend scanner.

Changed:

- `src/platforms/kwork_market.py`
  - Added default probe `telegram_budget30_low_offer` with `query=telegram`, `price_from=30000`, `kworks_filter_to=5`.
  - Raised default `get_buyer_scout(max_probes=...)` from `8` to `10` so zero-offer probes remain included.
- `src/api/routes/kwork.py`
  - Raised `KworkBuyerScoutRequest.max_probes` default from `8` to `10`.
- `tests/unit/test_kwork_market.py`
  - Added regression coverage for the budget Telegram probe and zero-offer probes.

Verification:

- `python -m pytest C:\psr\tests\unit\test_kwork_market.py C:\psr\tests\unit\test_kwork_routes.py -q` -> `37 passed`.
- `python -m ruff check C:\psr\src\platforms\kwork_market.py C:\psr\src\api\routes\kwork.py C:\psr\tests\unit\test_kwork_market.py` -> `All checks passed`.

## 2026-07-09 09:56 MSK - backend-only live market analysis rerun

Purpose: run the real market analysis from code only, without the desktop UI.

Artifacts:

- `docs/kwork_backend_code_market_analysis_20260709.md`
- `docs/kwork_market_snapshots/backend_code_market_analysis_20260709T065632Z.json`
- `docs/kwork_market_snapshots/kwork_buyer_scout_20260709T065646Z.json`

Runtime state:

- Session Hub was live on `127.0.0.1:8669` and returned `14` Kwork cookies.
- VPNTE proxy was live on `127.0.0.1:17990`.
- The backend script explicitly loaded `C:\psr\.env`; this fixed the previous empty buyer-scout run.

Live buyer-scout result:

- Probes: `10`.
- Unique buyer lots: `43`.
- Zero-offer lots: `8`.
- Low-offer lots: `32`.
- Top observed budget: `120000`.
- Endpoint errors: none.

Strongest probes:

- `телеграм бот`, `kworks_filter_to=5`: count `26`, sample `12`, all sampled lots low-offer.
- `телеграм бот`, `kworks_filter_to=0`: count `6`, sample `6`, all zero-offer.
- `telegram`, `price_from=30000`, `kworks_filter_to=5`: count `10`, sample `10`, all low-offer, 2 zero-offer.
- `python бот`, `kworks_filter_to=5`: count `14`, sample `12`, 3 zero-offer.
- `доработка сайта`, `kworks_filter_to=10`: count `33`, sample `12`, but broader/saturated supply.

Conclusion:

- Real market analysis must be buyer-first: ranked lots, budget, offer count, matched probe, and score reasons.
- Seller frequency chips remain only a weak supply/concentration signal and should not be presented as the main market analysis.

## 2026-07-09 10:14 MSK - captcha and buyer-flow preflight probe

Purpose: investigate the captcha/manual verification symptom without visual UI checks.

Artifacts:

- `docs/kwork_captcha_buyer_flow_probe_20260709.md`
- `docs/kwork_market_snapshots/kwork_captcha_buyer_flow_probe_20260709T070826Z.json`

Live findings:

- Session Hub was live and returned Kwork cookies.
- `POST /getCaptchaStatus` returned `success=true`, `response.show_captcha=false`.
- `GET /new_offer?project=3202311` returned HTTP `200` with title `Предложить услугу - Kwork`.
- The page included global Yandex SmartCaptcha script/config, but had no `data-sitekey` and no `/not_access.php`.
- `GET /wants/3202311/check_offer_notify` returned `success=true`, category `46`, classification `281`, and portfolio/kwork notification flags.
- `POST /projects/check_is_template` with Session Hub cookies returned `success=true`, `data=[]`.

Bug fixed:

- `KworkExtensions.get_captcha_status()` used to return `True` for any non-empty response dict. Live Kwork returned `{"show_captcha": false}`, so this caused false captcha positives.
- `src/platforms/kwork_listing.py` no longer treats global SmartCaptcha script/token markers on normal HTTP 200 pages as a manual verification page by themselves.

Verification:

- Targeted captcha tests: `4 passed`.
- Related unit set: `57 passed`.
- Ruff on modified source/listing test files: `All checks passed`.


## 2026-07-09 10:43 MSK - backend-only buyer-first market analysis rerun

Purpose: run real market analysis from code, not UI, focused on buyer lots and early low-offer opportunities.

Artifacts:

- `docs/kwork_backend_buyer_analysis_20260709.md`
- `docs/kwork_market_snapshots/backend_only_market_analysis_live_20260709T074305Z.json`
- `docs/kwork_market_snapshots/kwork_buyer_scout_20260709T074240Z.json`
- `docs/kwork_market_snapshots/kwork_market_intelligence_20260709T074305Z.json`

Live result:

- Buyer scout status: `ok`.
- Probes: `9`.
- Unique buyer lots: `46`.
- Zero-offer lots: `7`.
- Low-offer lots (`0..5` offers): `36`.
- Top observed budget: `200000` RUB.
- Median budget among ranked top lots: `25500` RUB.
- Endpoint errors: none.

Best current buyer lanes:

- `телеграм бот`, `kworks_filter_to=5`: count `22`.
- `telegram`, `price_from=30000`, `kworks_filter_to=5`: count `10`.
- `телеграм бот`, `kworks_filter_to=0`: count `3`.
- `сайт`, `kworks_filter_to=0`: count `5`.
- `доработка сайта`, `kworks_filter_to=10`: count `36`, but broader and more saturated.

Implementation fix made before the live run:

- `src/platforms/kwork_market.py` now preserves buyer identity/history from top-level project/want API fields: `username`, `user_id`, `user_projects_count`, `user_active_projects_count`, `user_hired_percent`, `buyer_projects_url`, `has_offer`, `is_viewed`.
- `tests/unit/test_kwork_market.py` covers this with a regression test based on live `/project` field shape.

Verification:

- `python -m pytest C:\psr\tests\unit\test_kwork_market.py C:\psr\tests\unit\test_kwork_routes.py -q` -> `38 passed`.
- `python -m ruff check C:\psr\src\platforms\kwork_market.py C:\psr\tests\unit\test_kwork_market.py C:\psr\src\api\routes\kwork.py` -> `All checks passed`.

Tavily search note:

- Tavily `search` for public Kwork projects/wants API references returned no useful API documentation; local Kwork JS bundles and live API probes remain the reliable source for hidden buyer-flow endpoints.


## 2026-07-09 11:30 MSK - buyer history integrated into scout and UI

Purpose: turn the discovered buyer-history page into a useful buyer-first signal in backend and desktop UI.

Discovery:

- `GET https://kwork.ru/projects/list/{username}` is a read-like buyer-history page.
- The useful data is embedded in `window.stateData.wants.data`.
- Old `KworkStateDataParser.projects_from_state()` only handled `stateData.wants` as a direct list or `pagination.data`, so it returned `0` projects for buyer history pages.

Code changes:

- `src/platforms/kwork.py`
  - `KworkStateDataParser.projects_from_state()` now accepts dict-shaped `wants` and reads `wants.data`, `wants.items`, or `wants.wants`.
  - Titles now go through `_strip_html`, so `&ndash;` becomes readable text.
- `src/platforms/kwork_market.py`
  - Added `fetch_buyer_history(username)` for `/projects/list/{username}` with Session Hub cookies.
  - Added `summarize_history_project()`.
  - `get_buyer_scout()` now supports `include_buyer_history` and capped `buyer_history_limit`.
- `src/api/routes/kwork.py`
  - `KworkBuyerScoutRequest` exposes `include_buyer_history` and `buyer_history_limit`.
- `desktop/src/lib/api.ts`
  - Request type updated.
- `desktop/src/pages/KworkMarket.tsx`
  - Market scan now requests `include_buyer_history=true` for top 6 buyer lots.
  - Buyer scout UI was changed from raw/English labels to Russian buyer-first cards: buyer username, total/active lots, hire percent, recent other lot titles, and separate links for buyer history and making an offer.

Live smoke:

- Artifact: `docs/kwork_market_snapshots/kwork_buyer_history_integration_smoke_20260709T082830Z.json`.
- Result: buyer scout `ok`; 3 probes; 9 unique projects; 2 zero-offer; 7 low-offer.
- `Salvador1w1332` history returned `4` projects, including:
  - `Массовый инвайтинг +рассылка с отчетом`
  - `Нужно создать 5–6 Telegram Stories с упоминанием`
  - `10000 упоминаний в telegram`

Verification:

- `python -m pytest C:\psr\tests\unit\test_kwork_market.py C:\psr\tests\smoke\test_kwork_state.py C:\psr\tests\unit\test_kwork_routes.py -q` -> `42 passed`.
- `python -m ruff check C:\psr\src\platforms\kwork.py C:\psr\src\platforms\kwork_market.py C:\psr\src\api\routes\kwork.py C:\psr\tests\unit\test_kwork_market.py C:\psr\tests\smoke\test_kwork_state.py C:\psr\tests\unit\test_kwork_routes.py` -> `All checks passed`.
- `npm run build` in `desktop` -> Vite build and electron-builder NSIS succeeded.
- Built installer: `desktop/dist-electron/PSR Desktop Setup 1.0.0.exe`.

Autopentest:

- Added graph node for `GET /projects/list/{username}` with parser path `window.stateData.wants.data`.
- Tracked `WSTG-APIT-02` as completed for this read-only buyer-history inventory.
- Saved updated `endpoint_map` deliverable.


## 2026-07-09 15:05 MSK - buyer scout market signals and clean competitor cards

Purpose: remove the remaining "raw data pretending to be analysis" feel from Kwork Market and make the market scan say what matters for finding buyers.

Code changes:

- `src/platforms/kwork_market.py`
  - Added `build_buyer_market_signals()` for buyer-first aggregates inside `get_buyer_scout()`.
  - `aggregate.market_signals` now highlights:
    - lots with zero offers;
    - high-budget lots with low offers;
    - buyers with repeated project history;
    - buyers with hiring percentage;
    - probe/filter windows that currently return live lots.
- `desktop/src/lib/api.ts`
  - Added optional `aggregate.market_signals` to the buyer scout type.
- `desktop/src/pages/KworkMarket.tsx`
  - Buyer scout now renders market signals as human cards above the lot list.
  - Competitor card cleanup now prefers human `volume_service_in_kwork` / `unit_and_quantity` values and includes `raw.kwork_description`.
  - Technical `volume_type_id/base_volume/volume_types` blobs are filtered before UI display.
- `tests/unit/test_kwork_market.py`
  - Regression now asserts that buyer scout returns the useful signal kinds.

Verification:

- `python -m pytest C:\psr\tests\unit\test_kwork_market.py C:\psr\tests\smoke\test_kwork_state.py C:\psr\tests\unit\test_kwork_routes.py -q` -> `42 passed`.
- `python -m ruff check C:\psr\src\platforms\kwork_market.py C:\psr\tests\unit\test_kwork_market.py` -> `All checks passed`.
- `npm run build` in `desktop` -> Vite build and electron-builder NSIS succeeded.
- Built installer: `desktop/dist-electron/PSR Desktop Setup 1.0.0.exe`.

Autopentest:

- Retrieved `WSTG-APIT-02` before API-related work in this turn.
- Tracked `WSTG-APIT-02` as completed for the local integration/test step over the already mapped buyer endpoints.


## 2026-07-09 12:08 MSK - checked POST /wants/portfolio candidate

Purpose: resolve the remaining JS-discovered `POST /wants/portfolio` candidate and decide whether it helps find buyers faster.

Source:

- `new-offer_88b52ade0a351544.js` calls `axios.post("/wants/portfolio", {userId, category, page, lang})`.
- JS values are `offerUserId`, `categoryId`, `currentPortfolioPage + 1`, and `wantLang`.

Live probe:

- `GET https://kwork.ru/new_offer?project=3202311` with Session Hub cookies returned HTTP `200`.
- Extracted `window.stateData` had `categoryId=46`, `offerParams.wantLang=ru`, `offerParams.kworks=[]`.
- `POST https://kwork.ru/wants/portfolio` with JSON `{category: 46, page: 1, lang: "ru"}` returned HTTP `200`, JSON `success=true`.
- Response data keys: `totalCount`, `offset`, `curCount`, `haveNext`, `portfolioJson`, `allPortfolioIds`.
- Tested account/category returned empty arrays.

Artifacts:

- `docs/kwork_market_snapshots/kwork_wants_portfolio_probe_20260709T090535Z.json`
- `docs/kwork_market_snapshots/kwork_wants_portfolio_actor_probe_20260709T090758Z.json`

Decision:

- This is a read-like offer-form helper for selecting portfolio examples by category.
- It is not a global buyer feed and should not be included in `Market scan`.
- Keep it documented for future offer/autopublish UX, where it can suggest portfolio attachments before sending a proposal.

Autopentest:

- Retrieved `WSTG-APIT-02` before the live probe.
- Added graph node `ep-post-wants-portfolio`.
- Saved updated `endpoint_map` deliverable.
- Tracked `WSTG-APIT-02` as completed for `GET /new_offer?project=3202311` and `POST /wants/portfolio`.


## 2026-07-09 12:26 MSK - projects filter matrix: web ids vs mobile API params

Purpose: verify whether Kwork web filter state can be sent directly to the mobile `POST /projects` API for faster buyer discovery.

Source state:

- `GET https://kwork.ru/projects` with Session Hub cookies exposed `wantsListData.filter` keys:
  - `price_from`
  - `price_to`
  - `hiring_from`
  - `kworks_filters`
  - `prices_filters`
  - `keyword`
- It also exposed UI filter boundary dictionaries:
  - `filters.by_kworks`
  - `filters.by_budget`

Live matrix result:

- `kworks_filter_to=5` works and narrowed baseline `499` wants to `188`.
- `price_from=30000` works and narrowed baseline `499` wants to `175`.
- `query=telegram` works and narrowed baseline `499` wants to `30`.
- Combined canonical filters `query=telegram`, `price_from=30000`, `kworks_filter_to=5` narrowed to `6` wants.
- Web UI fields were ignored by the mobile API:
  - `kworks_filters=0` returned the same count as baseline.
  - `prices_filters=4` returned the same count as baseline.
  - `keyword=telegram` returned the same count as baseline.

Artifacts:

- `docs/kwork_market_snapshots/kwork_projects_state_filters_20260709T091848Z.json`
- `docs/kwork_market_snapshots/kwork_projects_filter_boundaries_20260709T092103Z.json`
- `docs/kwork_market_snapshots/kwork_projects_filter_matrix_20260709T092609Z.json`

Decision:

- Buyer scout must keep using mobile API params:
  - `query`
  - `price_from`
  - `price_to`
  - `hiring_from`
  - `kworks_filter_from`
  - `kworks_filter_to`
- Do not send `keyword`, `kworks_filters`, or `prices_filters` to `POST /projects` / `POST /getWantsCount`.
- Web UI ids are still useful as a translation source:
  - `by_kworks id=0` means `kworks_filter_to=5`.
  - `by_budget id=4` means `price_from=30000`.

Implementation guard:

- Added unit coverage so `_buyer_probe_filters()` keeps only the canonical mobile API params and drops the ignored web fields.
- Added unit coverage for buyer project title cleanup of HTML tags/entities.


## 2026-07-09 13:13 MSK - captcha false positive split: API flag vs web challenge

Purpose: investigate why PSR could open the Kwork manual verification window while the Chrome/Session Hub browser did not show a captcha.

Live probe:

- `POST /getCaptchaStatus` through the Session Hub cookie-only API client did not prove captcha. Current response path raised `KworkException: Некорректные значения параметров`.
- `check_account_health()` before the fix returned `captcha_required=false` for the current account, but the code path still treated `getCaptchaStatus=true` as a possible manual-verification reason when orders existed.
- `KworkWebListingClient.open_new()` with Session Hub cookies opened `https://kwork.ru/new` and did not return `manual_verification_required`.
- No visual browser check was used.

Artifacts:

- `docs/kwork_market_snapshots/kwork_captcha_false_positive_probe_20260709T_current.json`
- `docs/kwork_market_snapshots/kwork_captcha_health_after_fix_20260709T_current.json`

Implementation:

- `src/platforms/kwork_ext.py`
  - Added `KworkExtensions.get_captcha_status_detail()`.
  - `AccountHealthMonitor.check()` now exposes:
    - `captcha_api_flag`
    - `captcha_status`
    - `captcha_check_error`
    - `manual_verification_required`
  - Health no longer treats a raw `getCaptchaStatus` flag as proven web manual verification.
- `desktop/src/components/Layout.tsx`
  - Health polling opens the Kwork verification window only when `manual_verification_required=true`.
- `desktop/src/lib/api.ts`
  - Added the new optional health fields.
- `desktop/src/pages/Health.tsx`
  - Shows `captcha_api_flag` separately as diagnostic `API flag only`.
- `tests/test_psr_features.py`
  - Added coverage for captcha status detail and the false-positive health split.

Verification:

- `python -m pytest C:\psr\tests\test_psr_features.py::TestKworkExtensions C:\psr\tests\test_psr_features.py::TestAccountHealthMonitor C:\psr\tests\unit\test_kwork_listing.py -q` -> `37 passed`.
- `python -m ruff check C:\psr\src\platforms\kwork_ext.py` -> `All checks passed`.
- `npm run build` in `desktop` -> Vite and electron-builder succeeded; installer rebuilt.

Known lint note:

- `python -m ruff check C:\psr\src\platforms\kwork_ext.py C:\psr\tests\test_psr_features.py` still reports pre-existing unrelated lint issues in `tests/test_psr_features.py` (`asyncio` unused, `pytest_asyncio` unused, old unused locals, broad `pytest.raises(Exception)`).


## 2026-07-09 14:17 MSK - buyer scout hints, budget cap, and fast-fail

Purpose: make buyer-side market scan useful for a new/low-review seller and prevent multi-minute stalls when the current Session Hub cookie-only project API is invalid.

Live facts:

- `POST https://kwork.ru/want-search/suggest` works with web Session Hub cookies and form field `query`.
- `GET /want-search/suggest` and wrong field names return maintenance/empty-style responses and are not useful.
- Useful suggestions returned for diverse queries:
  - `telegram`: `telegram`, `telegram ???`, `телеграм бот ии`, `Создать телеграм бота`
  - `site`: `site`, `web site`
  - `python`: `python`, `бот на python`, `telegram bot python`, `питон python`
  - `ai`: `ai`, `ai о`, `AI агент автоматизация`, `AI ИИ`
- Current Session Hub cookie-only calls to project APIs still log `Некорректные значения параметров`; buyer project rows are therefore unavailable in this session, but the scan now stops project fan-out after repeated slow empty responses.

Implementation:

- `src/platforms/kwork_market.py`
  - Added `fetch_want_search_suggestions()` and `build_buyer_query_suggestions()`.
  - Buyer scout defaults now use diverse probes instead of Telegram-only windows.
  - Default/test budget cap is `5000`, with hard upper bound `150000`.
  - `get_buyer_scout()` no longer calls `getWantsCount` before every project sample; it derives count from project response metadata and fast-fails after repeated slow empty project responses.
  - `_clean_text()` now repairs common Kwork mojibake from `cp1251` and `latin1` decode paths, so suggestion JSON and UI cards do not show `Р±Рѕ...`/`Ð...` garbage.
- `src/api/routes/kwork.py`
  - Buyer scout accepts `budget_max`, `include_query_suggestions`, and `query_suggestion_limit`.
- `desktop/src/pages/KworkMarket.tsx`
  - Market scan panel has configurable buyer budget cap.
  - Buyer scan result renders `/want-search/suggest` keyword hints.

Verification:

- `python -m pytest C:\psr\tests\unit\test_kwork_market.py C:\psr\tests\unit\test_kwork_routes.py -q` -> `43 passed`.
- `python -m ruff check C:\psr\src\platforms\kwork_market.py C:\psr\src\api\routes\kwork.py C:\psr\tests\unit\test_kwork_market.py C:\psr\tests\unit\test_kwork_routes.py` -> `All checks passed`.
- Live smoke saved `docs\kwork_market_snapshots\kwork_buyer_scout_20260709T111729Z.json`.
- Live smoke timing: `14471ms`, `2` project probes, fast-fail after repeated slow empty responses, `0` unique projects, keyword hints `ok`.

Decision:

- Keep buyer-scout useful even when project API is invalid: show hints and explicit endpoint errors instead of waiting minutes.
- Default to `budget_max=5000` for low-review/new-seller analysis; allow UI override up to `150000`.
- Do not rank high-budget buyer lots above the cap for the default new-seller workflow.


## 2026-07-09 14:35 MSK - token-required project guard for cookie-only Session Hub

Purpose: remove the remaining 10-15 second delay when buyer scout is launched while Session Hub provides only web cookies and no mobile token credentials.

Implementation:

- `src/platforms/kwork.py`
  - `KworkService.get_raw_projects()` now checks `api._psr_auth_mode`.
  - If auth mode is `cookie-only`, it returns immediately with metadata:
    - `auth_mode=cookie-only`
    - `token_required=true`
    - `detail=POST /projects requires token-mode auth...`
  - `KworkService.get_wants_count()` also returns `0` immediately for cookie-only token-required calls.
- `src/platforms/kwork_market.py`
  - Buyer scout treats this metadata as probe `status=skipped`.
  - Fan-out stops after the first skipped token-required project probe.
  - `/want-search/suggest` still runs, so the screen keeps useful buyer keyword hints.

Verification:

- `python -m pytest C:\psr\tests\unit\test_kwork_service.py::TestGetApiSessionHubPriority::test_token_required_project_calls_skip_cookie_only_client C:\psr\tests\unit\test_kwork_market.py::test_buyer_scout_marks_cookie_only_project_api_as_skipped C:\psr\tests\unit\test_kwork_market.py C:\psr\tests\unit\test_kwork_routes.py -q` -> `45 passed`.
- `python -m ruff check C:\psr\src\platforms\kwork.py C:\psr\src\platforms\kwork_market.py C:\psr\tests\unit\test_kwork_service.py C:\psr\tests\unit\test_kwork_market.py` -> `All checks passed`.
- Live smoke saved `docs\kwork_market_snapshots\kwork_buyer_scout_20260709T113539Z.json`.
- Live smoke timing improved to `2881ms`.
- Project probe result:
  - `status=skipped`
  - `token_required=true`
  - no slow `Некорректные значения параметров` request loop.
- Suggestions still returned for `telegram`, `site`, `python`, and `ai`.

Decision:

- Buyer lots from `POST /projects` require token-mode auth. With only browser cookies, PSR must skip quickly and show a clear status instead of treating the result as an empty market.


## 2026-07-09 14:59 MSK - web `/projects` stateData fallback finds real buyer lots

Purpose: avoid being blocked by mobile token auth and still find buyer lots quickly through the authenticated Kwork web session.

Discovery:

- Tavily confirmed public web project URLs like `https://kwork.ru/projects?c=80`.
- Kwork web page `GET /projects` includes `window.stateData`.
- Existing `KworkStateDataParser.projects_from_state()` already extracts useful project fields from that state.
- Web filter names differ from mobile API names:
  - mobile `query` -> web `keyword`
  - mobile `price_to` -> web `price-to`
  - mobile `price_from` -> web `price-from`
  - mobile `kworks_filter_to<=5` -> web `kworks-filters=0`
  - category -> web `c`
  - web also exposes `prices-filters` ids and `hiring-from`

Live probes:

- `GET /projects` with Session Hub cookies returned stateData in `390-890ms`.
- `GET /projects?keyword=telegram` narrowed total from `67` to `10`.
- `GET /projects?keyword=telegram&price-to=5000&kworks-filters=0` narrowed to `1`.
- `GET /projects?keyword=python&price-to=5000&kworks-filters=0` narrowed to `1`.
- `GET /projects?kworks-filters=0` returned `19` low-offer projects.
- `GET /projects?c=80&kworks-filters=0&prices-filters=1` returned `1`.

Artifacts:

- `docs/kwork_market_snapshots/kwork_web_projects_state_probe_20260709T_current.json`
- `docs/kwork_market_snapshots/kwork_web_projects_state_filter_inspect_20260709T_current.json`
- `docs/kwork_market_snapshots/kwork_web_projects_filter_matrix_20260709T_current.json`
- `docs/kwork_market_snapshots/kwork_web_projects_hyphen_filter_probe_20260709T_current.json`
- `docs/kwork_market_snapshots/kwork_buyer_scout_20260709T115910Z.json`

Implementation:

- `src/platforms/kwork.py`
  - Added `KworkService.get_web_raw_projects()`.
  - `get_raw_projects()` now uses web state fallback when auth mode is `cookie-only`.
  - `get_wants_count()` returns web pagination total for cookie-only mode.
  - Added mapping from mobile buyer probe filters to web query params.
- Existing buyer scout code now receives real project rows with `meta.source=web_state`; no UI special case is needed.

Verification:

- `python -m pytest C:\psr\tests\unit\test_kwork_service.py::TestGetApiSessionHubPriority::test_token_required_project_calls_use_web_state_for_cookie_only_client C:\psr\tests\unit\test_kwork_market.py C:\psr\tests\unit\test_kwork_routes.py -q` -> `45 passed`.
- `python -m ruff check C:\psr\src\platforms\kwork.py C:\psr\src\platforms\kwork_market.py C:\psr\tests\unit\test_kwork_service.py C:\psr\tests\unit\test_kwork_market.py` -> `All checks passed`.
- Live buyer scout after integration:
  - `status=ok`
  - `elapsed_ms=7499`
  - `probe_count=5`
  - `unique_projects=3`
  - all project probes used `source=web_state`
  - examples:
    - `Скрипт на n8n, либо любой другой ЯП`, `500 ₽`, `2` offers
    - `База инструментов Freedom/KASE + API`, `1000 ₽`, `5` offers
    - `Создать мессенджер`, `1000 ₽`, `5` offers

Decision:

- For buyer discovery, cookie-only Session Hub is no longer a blocker.
- Prefer mobile `POST /projects` when token-mode auth exists, but use web `GET /projects` stateData as the fast fallback.
- Keep default `budget_max=5000` and low-offer filters for new/zero-review seller workflow.


## 2026-07-09 15:30 MSK - bounded project pagination, request cache, text repair

Purpose: make buyer scout a little less shallow without bringing back multi-minute scans.

Implementation:

- `get_buyer_scout()` now accepts `project_page_limit` and clamps it to `1..3`.
- The desktop Market scan passes `project_page_limit=2`.
- Identical project requests inside one scout run are cached by categories/page/query/filters.
- Ranking now scores every project fetched by the bounded fan-out, while each probe row still keeps a compact display sample.
- Web-state and market text normalization now repairs both UTF-8 mojibake and CP1251-as-Latin1 mojibake.

Verification:

- `python -m pytest C:\psr\tests\unit\test_kwork_market.py C:\psr\tests\unit\test_kwork_routes.py C:\psr\tests\unit\test_kwork_service.py -q` -> `72 passed`.
- `python -m ruff check C:\psr\src\platforms\kwork.py C:\psr\src\platforms\kwork_market.py C:\psr\src\api\routes\kwork.py C:\psr\tests\unit\test_kwork_market.py C:\psr\tests\unit\test_kwork_routes.py C:\psr\tests\unit\test_kwork_service.py` -> `All checks passed`.
- Live buyer scout saved `docs/kwork_market_snapshots/kwork_buyer_scout_20260709T123044Z.json`.
- Live smoke result:
  - `status=ok`
  - `elapsed_ms=7021`
  - `probe_count=5`
  - `unique_projects=3`
  - all project probes used `source=web_state`
  - saved JSON contains repaired Unicode text for project titles, market signal labels, and query suggestions.

Decision:

- Keep `project_page_limit=2` in the desktop Market scan for now. It is still fast and gives room for page 2 when a query has more than one page.
- Do not enable details/history by default; those remain explicit heavier enrichments.


## 2026-07-09 16:00 MSK - buyer scout default probes and fast UI scan fixed

Purpose: stop the Market scan button from secretly doing heavy detail/history enrichment and make the default buyer search less narrow than Telegram/Python only.

Discovery:

- Tavily search surfaced public buyer exchange category URLs including `GET /projects?c=78`, `GET /projects?c=43`, `GET /projects?c=85`, `GET /projects?c=75`, and `GET /projects?c=80`.
- Live matrix showed category probes outperform narrow keyword probes for zero-review/low-budget scouting:
  - `c=41&price-to=5000&kworks-filters=0` returned `7` programming lots.
  - `c=85&price-to=5000&kworks-filters=0` returned `101` lots; first two pages produced many zero/low-offer rows.
  - `c=78&price-to=5000&kworks-filters=0` returned `1` video lot.
  - `c=80&price-to=5000&kworks-filters=0` returned `1` lot.
- A separate Unicode-escaped probe confirmed Russian query params work when the caller sends real Unicode: `телеграм бот` returned a valid encoded URL and `1` lot.

Implementation:

- Updated `DEFAULT_BUYER_SCOUT_PROBES` to start with proven category windows `41`, `85`, `78`, `80`, then mixed Russian/English query probes.
- Desktop `runMarketScan()` no longer forces `include_project_details=true` or `include_buyer_history=true`.
- `KworkBuyerScoutRequest` backend defaults now keep project/want details off.
- `KworkService.close()` now closes both `_api` and `_token_api`.
- FastAPI shutdown lifespan now closes the shared Kwork service.

Verification:

- Live buyer scout saved `docs/kwork_market_snapshots/kwork_buyer_scout_20260709T130000Z.json`.
- Live result with the same fast UI profile:
  - `status=ok`
  - `elapsed_ms=13182`
  - `probe_count=10`
  - `unique_projects=31`
  - `zero_offer_count=6`
  - `low_offer_count=25`
  - `query_suggestions=5`
  - no `Unclosed client session` warning after explicit service close in the smoke.

Decision:

- Keep details/history behind the existing details toggle. Fast Market scan should collect rows and signals first; enrichment can be requested separately.
- Keep `c=85` because it finds many low-offer lots quickly, but label it as a broad discovery window in later UI copy so it is not mistaken for a precise niche.


## 2026-07-09 16:35 MSK - Kwork captcha/manual verification diagnostic

Purpose: explain why the PSR verification window can open the user's Kwork session but show no captcha.

Live artifact:

- `docs/kwork_market_snapshots/kwork_verification_status_20260709T133248Z.json`

Live result:

- PSR route: `GET /api/kwork/verification-status`.
- Kwork API check: `POST /getCaptchaStatus` returned `KworkException: Некорректные значения параметров`.
- Web checks with Session Hub cookies:
  - `/` -> `200`
  - `/new` -> `200`
  - `/manage_kworks` -> `200`
  - `/projects` -> `200`
- `manual_verification_required=false`.
- `web_session_ok=true`.
- `smartcaptcha_scripts_seen=true`.
- `cookie_count=15`.
- Total diagnostic time: `7659ms`.

Implementation:

- Added backend route `/api/kwork/verification-status`.
- Added Health UI card `Kwork web verification` with status, cookie count, web session result, SmartCaptcha script marker, API flag/error, and checked page chips.
- Updated manual verification detector to also match real Russian robot-check phrases, not only existing mojibake patterns.
- Added tests for real Russian robot text and for distinguishing global SmartCaptcha scripts from an actual challenge.

Decision:

- Do not treat global SmartCaptcha script markers as proof that captcha is required.
- `getCaptchaStatus` is currently diagnostic context only; it is not enough to open a verification window or block workflows.
- A real manual verification state must be proven by checked web pages: challenge URL/status and detector evidence.

## 2026-07-09 18:16 MSK - final buyer scout UI summary

Purpose: finish the last interrupted buyer-summary pass and stop the long Kwork market goal with the app in a buildable state.

Implementation:

- Added `buyer_summary` to `get_buyer_scout()` aggregate.
- `buyer_summary` now contains:
  - `best_windows`: strongest `/projects` probe windows by total/sample count.
  - `best_projects`: compact top ranked buyer lots.
  - `next_actions`: explicit operational advice for fast replies.
  - `budget_fit_count`, `proven_buyer_count`, `zero_offer_count`, `low_offer_count`.
- Desktop Market UI now renders `Next actions` and `Best search windows` inside the buyer scout panel.
- Desktop API type now exposes `aggregate.buyer_summary`.
- Added unit coverage for `buyer_summary` in buyer scout ranking test.

Verification:

- `python -m ruff check src/platforms/kwork_market.py src/api/routes/kwork.py src/platforms/kwork_listing.py tests/unit/test_kwork_market.py` -> `All checks passed`.
- `python -m pytest tests/unit/test_kwork_market.py tests/unit/test_kwork_routes.py tests/unit/test_kwork_listing.py -q` -> `69 passed`.
- `python -m pytest tests/unit -q` -> `204 passed`.
- `npm run build` in `desktop` -> Vite build + electron-builder succeeded.
- Rebuilt installer: `desktop/dist-electron/PSR Desktop Setup 1.0.0.exe`, size `82357605`, timestamp `2026-07-09 18:15:42`.

Decision:

- Buyer scout should lead with actionable buyer lots, best search windows, and next actions.
- Seller/card counts can remain as optional supply context, but they are not the main buyer-market analysis.
- This is the final pass for the long-running goal unless the user explicitly starts a new task.

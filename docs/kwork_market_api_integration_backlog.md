# Kwork Market API Integration Backlog

Date: 2026-07-08 20:47 MSK

Last implementation update: 2026-07-08 21:20 MSK

Source documents:

- `docs/kwork_api_inspection_report.md`
- `docs/kwork_hidden_api_facts.md`
- Autopentest deliverable `endpoint_map`, saved 2026-07-08 20:43 MSK
- Autopentest priority queue, created 2026-07-08 20:46 MSK

This file is a practical PSR implementation backlog, not a security finding list. It maps validated Kwork APIs to what PSR currently uses and what should be wired next for whole-market seller, competitor, demand, and pricing intelligence.

## Current PSR Coverage

`src/platforms/kwork_market.py` already has the core market scanner shape:

- `get_market_intelligence_snapshot()` collects seeds, supply pages, optional demand, optional competitor details, optional seller details, rankings, and snapshot history.
- `get_catalog_market_seeds()` uses `POST /catalogMainv2`.
- `get_kworks()` uses `POST /kworks` with `categoryId`, `classifierId`, `page`.
- `get_demand_snapshot()` and `_get_projects_snapshot()` use `getWantsCount` / `projects` through `KworkService`.
- `fetch_competitor_detail()` uses `POST /getKworkDetails`, so HTML detail loading is already avoidable.
- `fetch_seller_intelligence()` uses `userByUsername`, `kworksCategoriesList`, and `userKworks`.
- `get_price_rules()` calls freeprice endpoints.
- `build_market_rankings()` scores supply/demand, sample price, and seller concentration.

## Autopentest Priority Queue

Top endpoints by autopentest scoring:

1. `POST /projects` - 23.0
2. `POST /kworks` - 20.5
3. `POST /portfolioList` - 20.5
4. `GET /api/attribute/loadclassification` - 20.0
5. `POST /userReviews` - 19.5
6. `POST /userKworks` - 17.5
7. `POST /getWantsCount` - 17.5
8. `POST /getKworkDetails` - 16.5
9. `POST /getKworkDetailsExtra` - 16.5
10. `POST /user` - 16.5
11. `GET /api/freeprice/categorygetprices` - 16.0
12. `POST /want` - 15.5

## Implementation Backlog

| Priority | API | Current PSR state | Gap | Next implementation step |
|---|---|---|---|---|
| P0 | `POST /projects` | Demand samples normalized on 2026-07-08; buyer scout probes added on 2026-07-09. | Done for canonical mobile filters; web UI fields `keyword`, `kworks_filters`, `prices_filters` are confirmed ignored by mobile API and must not be sent directly. | Preserve `offers`, `price`, `time_left`, `user_hired_percent`, `user_need_kwork`, `user_need_portfolio`, `allow_higher_price`, `possible_price_limit`; use `query`, `price_from/to`, `hiring_from`, `kworks_filter_from/to` for buyer-first ranking when token-mode auth is available. |
| P0 | `GET /projects` web stateData | Wired into `KworkService.get_raw_projects()` on 2026-07-09 for cookie-only Session Hub fallback. | Done for `keyword`, `c`, `price-to/from`, `kworks-filters`, `prices-filters`, `hiring-from`; later work can add deeper pagination fan-out. | Use when mobile token auth is unavailable; map mobile probe filters to web hyphen params and parse `window.stateData.pagination.data`. |
| P0 | `POST /want-search/suggest` | Wired into buyer scout on 2026-07-09 as fast keyword hints. | Done for canonical form field `query`; GET and alternative field names are not useful. | Keep hints visible even when project API returns no rows; clean mojibake through `_clean_text()`. |
| P0 | `POST /kworks` | Used as primary supply source; taxonomy seed discovery added on 2026-07-08. | Done for `catalogMainv2 + catalogRubrics/catalogCategories` merge; later work can add UI category/rubric filters. | Keep deepest `classifierId` for narrow scans; use taxonomy seeds for broader whole-market discovery. |
| P0 | `POST /portfolioList` | Wired into `fetch_seller_intelligence()` on 2026-07-08. | Done for first page; later work can add category-specific or deeper pagination. | Keep capped summary: total/pages, top portfolio examples, media counts, views, comments. |
| P0 | `POST /userReviews` | Wired into `fetch_seller_intelligence()` on 2026-07-08. | Done for first page of `all` and `negative`; later work can add deeper pagination for selected sellers. | Keep capped summary: totals/pages, review text snippet, linked kwork, negative answer flag. |
| P0 | `POST /getKworkDetailsExtra` | Wired into `fetch_competitor_detail()` on 2026-07-08. | Done for capped adjacent-kwork discovery; later work can expose this in UI on demand. | Keep summary of reviews/ratings/FAQ and capped recommended/similar/other kworks. |
| P1 | `GET /api/attribute/loadclassification` | Used elsewhere for form manifests. | Market scanner does not use it for exact UI field mapping in market filters. | Attach control metadata to market seeds when building classifier/filter fan-out; preserve real field names like `attribute[208]`. |
| P1 | `POST /catalogRubrics` + `POST /catalogCategories` | Validated, not wired in market seed discovery. | Whole-market scan misses complete root-to-child taxonomy and relies on curated `catalogMainv2`. | Add `get_catalog_taxonomy_seeds()` and merge with `catalogMainv2` seeds. |
| P1 | `GET /api/freeprice/categorygetprices` | Optional `include_price_rules` wiring added on 2026-07-08. | Done for snapshots/rankings; later work should expose in UI sliders. | Store `minPrice`, `maxPrice`, `typicalPriceGradation`; use in price opportunity scoring and UI sliders. |
| P1 | `POST /want` | Optional capped `include_want_details` wiring added on 2026-07-08. | Done for top demand samples; later work can select only low-offer/high-budget rows. | Enrich only top demand rows with `want(id)` for `views`, `orders`, and history. |
| P1 | `POST /actor`, `/getActorInfo`, `/kworksStatusList`, `/offers` | Optional `include_account_context` backend + route + UI toggle added on 2026-07-08. | Done for compact snapshot context; later UI can add a richer account-health panel. | Keep separate from public market ranking. |
| P2 | `POST /viewedCatalogKworks` | Validated, not wired. | Could bias scan if mixed with public data. | Store separately as `personal_interest_history`; never mix into whole-market ranking unless explicitly tagged. |
| P2 | Web helpers: `check_is_template`, `check_offer_notify`, `getkworksites` | Validated. | Useful for proposal/publishing flows, not core market scan. | Keep in proposal/autopublish helpers; expose only contextual warnings in Market UI. |
| P3 | `POST /wants/portfolio` | Validated 2026-07-09 from `new-offer` JS and live Session Hub probe. | Offer-form portfolio helper, not buyer discovery; tested category returned empty arrays. | Do not include in Market scan; later use only in offer/autopublish UX to suggest portfolio attachments. |
| P3 | Portfolio form loaders | Validated. | Publishing helpers only; no broad market value. | Do not include in scanner; keep docs for publish/portfolio workflows. |
| P3 | `attributegetprices` | Validated but returned null for tested inputs. | Conditional value. | Keep fallback; try only when UI controls imply attribute-specific pricing. |

## Recommended Next Code Order

1. Done 2026-07-08: upgrade `fetch_seller_intelligence()`:
   - Add `include_portfolio=True`, `include_reviews=True`.
   - Call `portfolioList(user_id, category_id=all, page=1)`.
   - Call `userReviews(user_id, type=all/negative, page=1)` when token is available.
   - Keep strict caps and summarize; do not attach huge raw payloads to snapshots.

2. Done 2026-07-08: upgrade `fetch_competitor_detail()`:
   - Keep current `getKworkDetails` baseline.
   - Add optional `getKworkDetailsExtra(id)` summary for adjacent competitor discovery.
   - Store counts and a capped list of recommended/similar/other kwork ids/titles/prices/workers.

3. Done 2026-07-08: add taxonomy seed discovery:
   - `catalogRubrics()` -> root ids.
   - `catalogCategories(rubricId)` -> child category ids and `kworks_count`.
   - Merge with `catalogMainv2` seeds and dedupe by `(category_id, classifier_id)`.

4. Done 2026-07-08: improve demand/opportunity scoring:
   - Preserve `offers`, `price`, `time_left`, `user_hired_percent`, `user_need_kwork`, `user_need_portfolio`.
   - Add low-offer and high-budget signals.
   - Optionally enrich only top demand rows with `want(id)` for `views`, `orders`, and history.

5. Done 2026-07-08: add leaf price rules:
   - Call `categorygetprices(categoryId)` for leaf seeds.
   - Use non-null `prices.minPrice`, `prices.maxPrice`, `typicalPriceGradation` in UI and ranking.

7. Done 2026-07-08: optional `/want` demand detail enrichment:
   - Add `include_want_details` and `want_detail_limit`.
   - Call `POST /want id=<want_id>` only for capped top demand samples.
   - Store views, orders, views_history count, dates, and a short description.

6. Add account context panel:
   - Use `actor`, `getActorInfo`, `kworksStatusList`, and `offers`.
   - Keep this separate from public market ranking.
   - Backend snapshot support is done through `include_account_context`; route and Kwork Market UI toggle are wired.

## Non-Goals For Market Scanner

- Do not call `portfolio/log_portfolio_view`; it is a view analytics endpoint.
- Do not use portfolio form loaders as market sources.
- Do not mix `viewedCatalogKworks` into whole-market scores without an explicit `personal` tag.
- Do not rely on `category_id` snake_case for `catalogFilters`; use `categoryId`.
- Do not use `attribute[...]` as the primary narrowing mechanism for `/kworks`; use deepest `classifierId`.

## Verification Gates For Implementation

- Unit tests for seller enrichment with mocked `portfolioList` and `userReviews`.
- Unit tests for competitor extra enrichment with `recommended_kworks`, `similar_kworks`, `other_kworks`.
- Unit tests for taxonomy seed merge/dedupe.
- Live smoke completed 2026-07-08 for taxonomy + supply only: `catalogMainv2/catalogRubrics/catalogCategories/kworks`, 5 seeds and 2 supply slices ok.
- Unit tests now cover optional `/want` enrichment and optional category price rules in market rankings.
- Unit tests now cover optional account context summary from `actor/getActorInfo/kworksStatusList/offers`.
- Route and desktop API types now pass `include_want_details`, `want_detail_limit`, `include_price_rules`, and `include_account_context`.
- Kwork Market UI has scan toggles for `prices`, `wants`, and `account`; category metrics remain fast by default.
- Buyer scout route and UI now pass configurable `budget_max`; default is `5000`, capped at `150000`.
- Buyer scout live smoke on 2026-07-09 fast-failed after two slow empty project probes in `14471ms` and still returned `/want-search/suggest` hints. Current Session Hub cookie-only project API still logs `Некорректные значения параметров`.
- Buyer scout live smoke on 2026-07-09 after token-required guard returned in `2881ms`: `POST /projects` probe is marked `skipped` with `token_required=true` when Session Hub is cookie-only, while `/want-search/suggest` still returns hints.
- Buyer scout live smoke on 2026-07-09 after web fallback returned in `7499ms`: 5 probes, `source=web_state`, `3` unique buyer projects, all under `5000 ₽` and up to `5` offers.
- Buyer scout live smoke on 2026-07-09 after bounded pagination/cache returned in `7021ms`: 5 probes, `source=web_state`, `project_page_limit=2`, `3` unique buyer projects, and repaired Unicode text in saved JSON.
- Buyer scout live smoke on 2026-07-09 after default probe/UI speed fix returned in `13182ms`: 10 probes, `project_page_limit=2`, `31` unique buyer projects, `6` zero-offer, `25` low-offer, details/history off by default.
- Token-required project APIs now use `KworkService.get_token_api()` so cookie-only Session Hub clients do not block direct email/password auth fallback.
- Live demand smoke completed 2026-07-08 with process-local token-mode credentials: `/getWantsCount`, `/projects`, `/want`, `/kworks`, and category price rules returned compact usable data.
- Full capped live smoke completed 2026-07-08 with `include_demand=true`, `include_want_details=true`, `include_price_rules=true`, `include_competitor_details=true`, `include_seller_details=true`, and `include_account_context=true`. Result: supply, competitor detail+extra, demand+want, price rules, seller portfolio/reviews, and account context all returned `ok`.
- Snapshot JSON must remain compact and redacted; no token, cookie, email, password, or phone values.
- Kwork verification diagnostic added 2026-07-09: `GET /api/kwork/verification-status` checks `getCaptchaStatus` plus web pages `/`, `/new`, `/manage_kworks`, `/projects`. Live result was `status=ok`, `manual_verification_required=false`, `web_session_ok=true`, SmartCaptcha scripts seen globally, and `getCaptchaStatus` invalid-params. Health UI now shows this instead of leaving the user with an empty verification window.

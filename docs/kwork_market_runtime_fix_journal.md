# Kwork Market Runtime Fix Journal

Date: 2026-07-08

## What broke in the exe

- The packaged frontend was newer than the backend process listening on `127.0.0.1:7788`.
- The old uvicorn process was started before the Kwork Market routes were added, so `Market scan` called `/api/kwork/market/intelligence-snapshot` and received `HTTP 404: Not Found`.
- Electron now checks the required backend routes and kills/restarts a stale PSR backend on the port before launching the UI.

## What made Market slow

- `KWORK_PACING=true` globally patches `KworkAPI.request` and adds human-like delays for authenticated Kwork automation.
- Kwork Market is a read-only analytics screen and mostly uses public market endpoints, so those delays were unnecessary there.
- Market requests now pass `_psr_skip_pacing` when the global pacing patch is present.

## VPNTE finding

- The app `.env` can enable `VPNTE_PROXY_ENABLED=true` and `VPNTE_PROXY_STRICT=true`.
- After switching VPN profiles, the local listener `http://127.0.0.1:17990` may be open while upstream Kwork requests still time out.
- Kwork Market now ignores the global VPNTE proxy by default. It only uses VPNTE if `KWORK_MARKET_USE_PROXY=true`.
- If proxy is enabled for Market, `KWORK_MARKET_PROXY_STRICT=false` allows a direct fallback when proxy lookup fails.

## Current fast path

- Regular category/competitor metrics use `POST https://api.kwork.ru/kworks`.
- Deep competitor details use `POST https://api.kwork.ru/getKworkDetails` only when the UI `details` toggle is enabled.
- Market scan uses explicit UI seeds, so the backend no longer spends time discovering catalog seeds before a scan.
- Heavy scan options are separate toggles: prices, wants, account, sellers.

## Verification checklist

- Restart backend after code changes; stale uvicorn on `7788` invalidates UI checks.
- Measure quick scan with heavy flags disabled first.
- Measure `prices`, `wants`, and seller/account details separately because they use different upstream endpoints and latency profiles.
- Rebuild the exe only after backend route check, live timings, tests, and frontend build are green.

## 2026-07-08 UX follow-up

- Competitor descriptions were not actually "loading" in the fast mode. The fast `/kworks` cards often do not include full descriptions, and details are intentionally off for speed.
- UI now shows a truthful status: descriptions are off for speed unless the user enables the Russian `описания` toggle.
- Kwork Market now tries to display short descriptions from the raw `/kworks` card when Kwork provides them.
- The AI sparkle action no longer reloads the whole Kwork form manifest after every suggestion. It reloads only when the selected option touches a dynamic child field.
- Sparkle can run in fast heuristic mode when the `ИИ` toggle is off.
- Electron does not support `window.prompt()` in this app context, so live publish now uses an in-page confirmation block.
- The `Для кого` field is optional. If it is empty, frontend sends an empty audience and backend prompt rules tell the LLM not to invent an `auditory` value.
- Kwork cover generation default quality was raised from `low` to `medium`; env vars can still override it.
- Kwork Market screen state is persisted in `localStorage` under `psr:kwork-market:v3`, so leaving the page no longer clears draft progress.

## 2026-07-08 competitor descriptions cleanup

- The `описания` toggle used to request only `competitor_detail_limit=2`, so only the first two visible cards received `getKworkDetails` descriptions.
- The frontend now requests details for the six cards it renders in the competitor grid.
- Kwork can return `kwork_description` as escaped HTML (`&lt;p&gt;...`) and volume data as a dict-like object. Backend `_clean_text` now decodes entities before stripping HTML and extracts `volume_service_in_kwork` from volume dictionaries.
- The card UI also has a defensive sanitizer for stale cached/raw values, so `<p>` tags and `volume_type_id/base_volume` blobs are not shown to the user.
- Live smoke with category `11`, `include_competitor_details=true`, `competitor_detail_limit=6`: 6 cards returned `detail_status=ok`; no `<p>`, `&lt;`, `volume_type_id`, or `base_volume` markers in displayed service/description text; total time about 4.7s.

## 2026-07-09 state and scan labels follow-up

- Runtime logs still showed `limit=2` when `details` was enabled because backend defaults were still `competitor_detail_limit=2`. Defaults are now `6` in the API request model, GET route, and market client, matching the six visible competitor cards.
- The market snapshot was showing raw chips like `seed: - / -` and `seller: 2`. Empty opportunity rows are now filtered out, opportunity chips are rendered as labeled text (`балл`, `спрос`, `конкурентов`), and seller chips say how many cards were seen.
- Returning from another app section could overwrite restored Kwork form state because the page auto-called `loadFormManifest({}, undefined)` on initial mount. The initial manifest reload is now skipped when saved form/draft state exists.
- localStorage writes now compact large scan/form objects and retry, so a large market snapshot no longer silently prevents draft/settings persistence.
- Last metrics are also cached so the page has a useful view immediately while a fresh refresh runs.
- Demand lookup in metrics is capped by `KWORK_MARKET_DEMAND_TIMEOUT` (default `8s`), so a slow Kwork wants endpoint no longer blocks the market screen for ~50 seconds.
- Live smoke for category `38`, `include_demand=true`, `include_competitor_details=true`, no explicit detail limit: 6 descriptions enriched, demand timed out at about 8s, total about 12.9s instead of ~49.8s.

## 2026-07-09 code-only market analysis and UI fix

- Ran live backend analysis through `KworkMarketClient.get_market_metrics()` without driving the app UI. The probe used broad category slices plus max-to-min classifier slices, with `include_competitor_details=true` and `competitor_detail_limit=6`.
- The code returns much more than the UI previously showed: `kworks_count`, classifier counts, price min/median/max, review trust threshold, repeated seller concentration, frequent title/description terms, recommendations, and `timings_ms`.
- Category `38` (`Доработка и настройка сайта`): broad slice `13,626` kworks, top price median `1,250 ₽`, `10/10` sampled cards with `100+` reviews, frequent terms `битрикс`, `wordpress`, `php`. Classifier slices included `Доработка сайта` `7,472`, `Настройка сайта` `1,697`, `Исправление ошибок` `1,386`, `Ускорение сайта` `829`.
- Category `37` (`Создание сайта`): broad slice `24,155` kworks, median `10,000 ₽`, `10/10` sampled cards with `100+` reviews, terms `лендинг`, `tilda`, `wordpress`. `Новый сайт` had `23,174`; `Копия сайта` had `1,020` with median `750 ₽`.
- Category `41` (`Скрипты, боты и mini apps`): broad slice `38,117` kworks, median `1,750 ₽`, `8/10` sampled cards with `100+` reviews, terms `скрипт`, `бота`, `php`, `калькулятор`. Several classifier probes then hit Kwork `HTTP 403`, so results were kept as partial instead of failing the whole analysis.
- Category `25` (`Логотип и брендинг`), after rotating through VPNTE proxy path: broad slice `46,182` kworks, median `8,500 ₽`, `9/10` sampled cards with `100+` reviews, terms `логотип`, `дизайн`, `брендбук`. Classifier slices: `Логотипы` `33,600`, `Визитки` `7,872`, `Фирменный стиль` `4,157`, `Брендирование и сувенирка` `593`.
- Category `59` (`Ссылки`): broad slice `45,466` kworks, median `5,500 ₽`, `9/10` sampled cards with `100+` reviews, terms `ссылок`, `форумах`, `вечных`. Classifier slices: `Статейные и крауд` `21,928`, `В профилях` `9,707`, `Каталоги сайтов` `2,282`, `В комментариях` `2,191`.
- Category `74` (`Продающие и бизнес-тексты`): broad slice `13,053` kworks, median `1,250 ₽`, `10/10` sampled cards with `100+` reviews, terms `продающие`, `тексты`, `напишу`. Its classifier probes hit Kwork `HTTP 403` after the broad result.
- All successful probes reported `raw_bad_count=0`, confirming backend `_clean_text` now removes escaped HTML and `volume_type_id/base_volume` blobs from enriched descriptions/service sizes.
- UI fix: Kwork Market now renders `market_insights` as `Что видно по рынку` and `Что делать с кворком`, showing price range/median, trust threshold, concentration, frequent words, recommendations, and large/narrow classifier slices.
- UI fix: visible seller nickname chips were removed from the main market snapshot because they are diagnostic concentration evidence, not a market analysis by themselves.
- UI fix: frontend `cleanCompetitorText()` now mirrors backend cleanup for stringified volume dictionaries and returns an empty string when `volume_service_in_kwork` is empty.
- Evidence files: `docs/kwork_market_code_analysis_latest.json`, `docs/kwork_market_code_analysis_20260708T213752Z.json`, and `docs/kwork_market_code_analysis_continued_20260708T214135Z.json`.

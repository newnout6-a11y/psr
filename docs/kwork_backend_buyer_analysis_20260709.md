# Kwork buyer-first market analysis - 2026-07-09 10:43 MSK

Purpose: run market analysis from backend code only, without desktop UI and without visual browser checks. The goal is to find buyer lots faster than other sellers, not to count seller cards.

Artifacts:
- `C:\psr\docs\kwork_market_snapshots\backend_only_market_analysis_live_20260709T074305Z.json`
- `C:\psr\docs\kwork_market_snapshots\kwork_buyer_scout_20260709T074240Z.json`
- `C:\psr\docs\kwork_market_snapshots\kwork_market_intelligence_20260709T074305Z.json`

Live buyer scout result:
- Probes: `9`.
- Unique buyer lots: `46`.
- Zero-offer lots: `7`.
- Low-offer lots (`0..5` offers): `36`.
- Top observed budget: `200 000` RUB.
- Median budget among ranked top lots: `25 500` RUB.
- Endpoint errors: `0`.
- Timings: buyer scout `19581` ms, supply/demand snapshot `25237` ms.

Best buyer probes:

| Probe | Query | Filters | Count | Sample | Time |
|---|---|---|---:|---:|---:|
| `telegram_bot_low_offer` | `телеграм бот` | `kworks_filter_to=5` | 22 | 12 | 4531 ms |
| `telegram_low_offer` | `telegram` | `kworks_filter_to=5` | 15 | 12 | 1362 ms |
| `telegram_budget30_low_offer` | `telegram` | `price_from=30000, kworks_filter_to=5` | 10 | 10 | 938 ms |
| `automation_low_offer` | `автоматизация` | `kworks_filter_to=5` | 4 | 4 | 803 ms |
| `wordpress_low_offer` | `wordpress` | `kworks_filter_to=5` | 3 | 3 | 791 ms |
| `programming_low_offer` | `` | `kworks_filter_to=5` | 14 | 12 | 843 ms |
| `website_maintenance_low_offer` | `доработка сайта` | `kworks_filter_to=10` | 36 | 12 | 930 ms |
| `telegram_bot_zero_offer` | `телеграм бот` | `kworks_filter_to=0` | 3 | 3 | 870 ms |
| `site_zero_offer` | `сайт` | `kworks_filter_to=0` | 5 | 5 | 835 ms |

Top buyer lots right now:

| ID | Offers | Budget | Buyer | Buyer history | Probe | Score reasons | Title |
|---:|---:|---:|---|---|---|---|---|
| 3202311 | 0 | 90 000 | [Salvador1w1332](https://kwork.ru/projects/list/Salvador1w1332) | projects=139, active=4, hire=37% | `telegram_bot_low_offer` | 0 offers, high budget, higher price allowed, buyer has hiring history | Нужно создать 5–6 Telegram Stories с упоминанием |
| 3211575 | 0 | 120 000 | [romaignatenko](https://kwork.ru/projects/list/romaignatenko) | projects=24, active=1, hire=44% | `telegram_bot_low_offer` | 0 offers, high budget, higher price allowed, buyer has hiring history | Поиск клиентов постоянно |
| 3192226 | 0 | 90 000 | [Geo_Sokol](https://kwork.ru/projects/list/Geo_Sokol) | projects=152, active=1, hire=37% | `telegram_bot_low_offer` | 0 offers, high budget, higher price allowed, buyer has hiring history | Поиск заказов |
| 2966080 | 0 | 200 000 | [DXB-1](https://kwork.ru/projects/list/DXB-1) | projects=20, active=6, hire=56% | `site_zero_offer` | 0 offers, high budget, higher price allowed, buyer has hiring history | Нужны квал лиды |
| 3212022 | 1 | 30 000 | [Salvador1w1332](https://kwork.ru/projects/list/Salvador1w1332) | projects=139, active=4, hire=37% | `telegram_low_offer` | 1-2 offers, high budget, higher price allowed, buyer has hiring history | Массовый инвайтинг +рассылка с отчетом |
| 3204499 | 1 | 90 000 | [Salvador1w1332](https://kwork.ru/projects/list/Salvador1w1332) | projects=139, active=4, hire=37% | `telegram_low_offer` | 1-2 offers, high budget, higher price allowed, buyer has hiring history | 10000 упоминаний в telegram |
| 3191485 | 1 | 90 000 | [Salvador1w1332](https://kwork.ru/projects/list/Salvador1w1332) | projects=139, active=4, hire=37% | `telegram_budget30_low_offer` | 1-2 offers, high budget, higher price allowed, buyer has hiring history | ТГ упоминания в сторис |
| 3203050 | 2 | 90 000 | [kolek0969](https://kwork.ru/projects/list/kolek0969) | projects=16, active=4, hire=23% | `telegram_bot_low_offer` | 1-2 offers, high budget, higher price allowed | Массовые отметки пользователей в Telegram Stories |
| 3203045 | 1 | 99 000 | [kolek0969](https://kwork.ru/projects/list/kolek0969) | projects=16, active=4, hire=23% | `telegram_bot_low_offer` | 1-2 offers, high budget, higher price allowed | Отметки пользователей в Telegram Stories |
| 3204600 | 2 | 90 000 | [kolek0969](https://kwork.ru/projects/list/kolek0969) | projects=16, active=4, hire=23% | `telegram_bot_low_offer` | 1-2 offers, high budget, higher price allowed | Вывести канал в ТОП поиска малоизвестной тематике |
| 3211916 | 1 | 75 000 | [mediahelp247](https://kwork.ru/projects/list/mediahelp247) | projects=1, active=1, hire=None% | `telegram_bot_low_offer` | 1-2 offers, high budget, higher price allowed | Поиск заказчиков на услуги маркетинга |
| 3210268 | 1 | 90 000 | [xena_lav](https://kwork.ru/projects/list/xena_lav) | projects=1, active=1, hire=None% | `website_maintenance_low_offer` | 1-2 offers, high budget, higher price allowed | Необходимо найти клиентов на разработку сайтов |

Supply vs demand snapshot:

| Segment | Seller kworks | Buyer wants | Sample median seller price | Max allowed category price |
|---|---:|---:|---:|---:|
| Telegram classifier | 8772 | 41 | 1 500 | 20 616 |
| Programming broad | 38165 | 49 | 1 750 | 71 649 |
| Site support | 18261 | 49 | 2 250 | 71 649 |
| Ready databases | 34244 | 3 | 2 250 | 19 995 |
| Marketplaces | 109387 | 22 | 750 | 25 349 |

Query-demand checks without low-offer filters:

| Query | Wants | Sample | Low-offer sample | Zero-offer sample |
|---|---:|---:|---:|---:|
| `telegram` | 34 | 12 | 3 | 0 |
| `телеграм бот` | 55 | 12 | 0 | 0 |
| `python бот` | 32 | 12 | 1 | 0 |
| `бот` | 27 | 12 | 1 | 0 |
| `automation` | 0 | 0 | 0 | 0 |
| `доработка сайта` | 100 | 12 | 1 | 0 |
| `wordpress` | 21 | 12 | 2 | 0 |
| `bitrix` | 3 | 3 | 1 | 0 |

What actually finds buyers:

- Main fast source: `POST https://api.kwork.ru/projects` plus `POST https://api.kwork.ru/getWantsCount`.
- Useful buyer filters confirmed live: `categories`, `page`, `query`, `price_from`, `price_to`, `hiring_from`, `kworks_filter_from`, `kworks_filter_to`.
- Best current lanes: `телеграм бот + kworks_filter_to=5`, `telegram + price_from=30000 + kworks_filter_to=5`, `телеграм бот + kworks_filter_to=0`, and `сайт + kworks_filter_to=0` for early zero-offer lots.
- Buyer quality should use `username`, `user_id`, `user_projects_count`, `user_active_projects_count`, `user_hired_percent`, budget, offer count, and whether higher price is allowed.
- `POST /kworks` is still useful only for seller-side supply pressure and competitor examples. It is not enough to find buyers.

Endpoint inventory from Kwork JS / live probes:

- Read/market: `POST /projects`, `POST /getWantsCount`, `POST /project`, `POST /want`, `GET /projects/list/{username}`.
- Offer preflight/read-like: `GET /new_offer?project={id}`, `GET /wants/{id}/check_offer_notify`, `POST /projects/check_is_template`, `POST /getCaptchaStatus`.
- Utility/read-like candidate from JS, not yet integrated: `POST /wants/portfolio` for seller portfolio context during offer flow.
- State-changing endpoints found in JS and intentionally not called in passive market scans: `POST /api/offer/createoffer`, `POST /api/offer/editoffer`, `POST /api/offer/addview`, `POST /wants/hide_want`, `POST /wants/review/save`, `POST /projects/manage/restart`, `POST /wants/create_offer_draft`.

Captcha/manual-verification note:

- `POST /getCaptchaStatus` returned `show_captcha=false` in the live probe.
- `GET /new_offer?project=3202311` returned normal HTTP 200 offer page. The page has global SmartCaptcha scripts, but no active captcha widget marker (`data-sitekey`) and no `/not_access.php`.
- The backend bug was not Kwork captcha itself: PSR treated a non-empty response dict `{"show_captcha": false}` as truthy. That parser was fixed in `src/platforms/kwork_ext.py`; listing detection now ignores global SmartCaptcha scripts unless an actual challenge marker exists.

UI implication:

- The repeated seller chips like `OlegTixomirov · 2 карточки` are not market analysis. They only say that the sampled seller cards repeat the same worker.
- The real market-analysis panel should show ranked buyer lots first: title, budget, offers, buyer history, matched probe, score reasons, and project/buyer links.
- If buyer APIs return zero while `/kworks` works, UI should say session/token for buyer endpoints is missing or blocked, not ?no demand?.
